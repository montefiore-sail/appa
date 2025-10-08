r"""Script for all-at-once forecasting experiments."""

import copy
import gc
import h5py
import math
import numpy as np
import os
import re
import shutil
import sys
import torch
import torch.distributed as dist
import wandb
import warnings

from datetime import datetime, timedelta
from dawgz import after, job, schedule
from einops import rearrange
from functools import partial
from math import ceil, floor
from omegaconf import OmegaConf
from pathlib import Path

from appa.config import PATH_ERA5, PATH_STAT
from appa.config.hydra import compose
from appa.data.const import (
    CONTEXT_VARIABLES,
    DATASET_DATES_TEST,
    ERA5_PRESSURE_LEVELS,
    ERA5_VARIABLES,
    SUB_PRESSURE_LEVELS,
)
from appa.data.datasets import ERA5Dataset, LatentBlanketDataset
from appa.data.transforms import StandardizeTransform
from appa.date import create_trajectory_timestamps
from appa.diffusion import Denoiser, MMPSDenoiser, TrajectoryDenoiser, create_schedule
from appa.math import str_to_ids
from appa.observations import (
    create_masks,
    create_variable_mask,
    mask_state,
    observator_full,
    observator_partial,
)
from appa.sampling import select_sampler
from appa.save import load_auto_encoder, load_denoiser, safe_load, safe_save


def schedule_jobs(config, unique_id):
    target_dir = Path(config.model_path) / "forecast_aao" / unique_id

    # Save config.yaml in target_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_config = copy.deepcopy(config)
    saved_config.unique_id = unique_id
    OmegaConf.save(saved_config, target_dir / "config.yaml")

    # Launch job array of n samples for each window, according to a list of n timestamps
    assimilation_lengths = str(config.pop("assimilation_lengths"))
    lead_times = str(config.pop("lead_times"))
    blanket_size = compose(f"{config.model_path}/config.yaml").train.blanket_size
    overlap = config.blanket_overlap
    blanket_stride = blanket_size - overlap

    hardware_cfg = config.pop("hardware")
    gen_cfg = hardware_cfg.gen
    gen_gpus = gen_cfg.pop("gpus")

    num_samples_per_date = config.pop("num_samples_per_date")
    start_dates = config.pop("start_dates")
    start_dates = [start_date for start_date in start_dates for _ in range(num_samples_per_date)]
    print("start_dates", start_dates)
    start_dates = [
        (day, int(hour.split("h")[0])) for date in start_dates for day, hour in [date.split(" ")]
    ]
    start_dates, start_hours = zip(*start_dates)
    num_samples_per_traj_size = len(start_dates)

    jobs = []
    for assimilation_length in str_to_ids(ids_str=assimilation_lengths):
        for lead_time in str_to_ids(lead_times):
            # TODO: External function to compute padded traj size
            #       given desired (ie unpadded) traj size, blanket size, num of gpus or "auto", etc.
            #       - auto mode when given maxblankets_per_gpu: compute gpus needed.
            #       - if given num of gpus, then determine padded traj size (& blankets per gpu)

            trajectory_size = max(blanket_size, assimilation_length + lead_time)

            # Pad to get a valid number of blankets.
            while (trajectory_size - blanket_size) % blanket_stride != 0:
                trajectory_size += 1

            num_gpus = gen_gpus
            gpus_per_node = hardware_cfg.gpus_per_node

            num_blankets = (trajectory_size - blanket_size) // blanket_stride + 1
            if num_gpus > num_blankets:
                warnings.warn(
                    f"Lowering number of GPUs from {num_gpus} to {num_blankets}.",
                    stacklevel=2,
                )
                num_gpus = num_blankets

            if num_gpus > gpus_per_node:
                if ceil(num_gpus / gpus_per_node) * gpus_per_node <= num_blankets:
                    num_nodes = ceil(num_gpus / gpus_per_node)
                else:
                    num_nodes = floor(num_blankets / gpus_per_node)
                num_gpus = gpus_per_node
            else:
                num_nodes = 1

            if num_nodes > 1:
                interpreter = f"torchrun --nnodes {num_nodes} --nproc-per-node {num_gpus} --rdzv_backend=c10d --rdzv_endpoint=$SLURMD_NODENAME:12345 --rdzv_id=$SLURM_JOB_ID"
            else:
                interpreter = f"torchrun --nnodes 1 --nproc-per-node {num_gpus} --standalone"

            run_name = f"{assimilation_length}_{lead_time}h"

            window_target_dir = target_dir / run_name

            # Unique seed for the trajectory (for splitting masks)
            split_seed = np.random.randint(0, 2**32 - 1)

            # TODO: Make slurm utils file slurm.py and put many functions repeated across scripts in it.
            ram = re.search("[0-9]+", gen_cfg.ram).group()
            ram = str(int(ram) * num_gpus) + gen_cfg.ram.replace(ram, "")

            @job(
                name=f"appa forecast aao (gen {run_name})",
                nodes=num_nodes,
                gpus=num_gpus,
                array=num_samples_per_traj_size,
                interpreter=interpreter,
                cpus=num_gpus * gen_cfg.cpus,
                ram=ram,
                time=gen_cfg.time,
                partition=gen_cfg.partition,
            )
            def gen_trajectory(
                i: int,
                assimilation_length: int = assimilation_length,
                unpadded_trajectory_size: int = assimilation_length + lead_time,
                padded_traj_size: int = trajectory_size,
                window_target_dir: Path = window_target_dir,
                split_seed: int = split_seed,
            ):
                target_dir_traj = window_target_dir / f"tmp_{i}"
                target_dir_traj.mkdir(parents=True, exist_ok=True)

                forecast_aao(
                    assimilation_length=assimilation_length,
                    unpadded_trajectory_size=unpadded_trajectory_size,
                    padded_trajectory_size=padded_traj_size,
                    start_date=start_dates[i],
                    start_hour=start_hours[i],
                    target_dir=target_dir_traj,
                    split_seed=split_seed,
                    **config,
                )

            @after(gen_trajectory)
            @job(
                name=f"appa forecast aao (agg {run_name})",
                **hardware_cfg.aggregate,
            )
            def aggregate(window_target_dir=window_target_dir):
                if not config.observe_full_states:
                    all_masks = h5py.File(window_target_dir / "masks.h5", "w")
                    os.makedirs(window_target_dir / "masks", exist_ok=True)

                trajectories = []
                timestamps = []
                for i in range(num_samples_per_traj_size):
                    target_dir_traj = window_target_dir / f"tmp_{i}"
                    trajectory = safe_load(target_dir_traj / "trajectory.pt")
                    timestamp = safe_load(target_dir_traj / "timestamps.pt")

                    trajectories.append(trajectory)
                    timestamps.append(timestamp)

                    if not config.observe_full_states:
                        # TODO: Format masks to (d s) ... vs d s ...
                        shutil.move(
                            target_dir_traj / "masks.h5", window_target_dir / "masks" / f"{i}.h5"
                        )
                        all_masks[f"masks_{i}"] = h5py.ExternalLink(f"masks/{i}.h5", "/")
                        start_date_str = [str(e) for e in timestamp[0, 0].tolist()]
                        start_date_str = "-".join(start_date_str[:3]) + f" {start_date_str[3]}h"
                        all_masks[f"masks_{i}"].attrs["start_date"] = start_date_str

                    shutil.rmtree(target_dir_traj)
                    del trajectory, timestamp
                    gc.collect()

                if not config.observe_full_states:
                    all_masks.close()

                trajectories = torch.cat(trajectories)
                timestamps = torch.cat(timestamps)

                trajectories = rearrange(
                    trajectories, "(d s) ... -> d s ...", s=num_samples_per_date
                )
                timestamps = rearrange(timestamps, "(d s) ... -> d s ...", s=num_samples_per_date)

                safe_save(trajectories, window_target_dir / "trajectories.pt")
                safe_save(timestamps, window_target_dir / "timestamps.pt")

                print("Saved to", window_target_dir)

            jobs.append(aggregate)

    schedule(
        *jobs,
        name="appa forecast aao",
        account=hardware_cfg.account,
        backend=hardware_cfg.backend,
        env=[
            f"export OMP_NUM_THREADS={gen_cfg.cpus}",
            "export WANDB_SILENT=true",
            "export XDG_CACHE_HOME=$HOME/.cache",
            "export TORCHINDUCTOR_CACHE_DIR=$HOME/.cache/torchinductor",
        ],
    )


def forecast_aao(
    model_path,
    model_target,
    latent_dump_name,
    observe_full_states,
    diffusion,
    assimilation_length,
    unpadded_trajectory_size,
    padded_trajectory_size,
    split_seed,
    observed_variables,
    masks,
    blanket_overlap,
    start_date,
    start_hour,
    precision,
    target_dir,
):
    r"""Performs all-at-once forecasting.

    Args:
        model_path: Path to the model directory.
        model_target: Target model to load (best or last).
        latent_dump_name: Name of the latent dump h5 file.
        observe_full_states: Boolean indicating whether to observe full latent or partial pixel states.
        diffusion: Diffusion configuration dictionary:
            - num_steps: Number of diffusion steps.
            - mmps_iters: Number of iterations for MMPS.
            - sampler: Sampler configuration dictionary:
                - type: Type of sampler (pc or lms).
                - config: Configuration for the sampler.
        assimilation_length: Length of the assimilation window.
        unpadded_trajectory_size: Size of the trajectory to save.
        padded_trajectory_size: Size of the trajectory, padded to fit N blankets.
        split_seed: Seed for splitting masks, if observing partial states.
        observed_variables: Observed variables configuration dictionary if observing partial states.
            - stations: Dictionary with low and high (included) indices for station variables.
            - satellites: Dictionary with low and high (included) indices for satellite variables.
        masks: Masks configuration if observing partial states.
            Example:
            - name: leo
                type: satellite
                covariance: 1e-2
                config:
                orbital_altitude: 800
                inclination: 75
                initial_phase: 0
                obs_freq: 60
                fov: 5
            - name: weather11k
                type: stations
                covariance: 1e-4
                config:
                num_stations: 11k (or 5k)
                num_valid_stations: 0
        blanket_overlap: Overlap between consecutive blankets.
        start_date: Date of the first predicted state (YYYY-MM-DD format).
        start_hour: Hour of the first predicted state (0-23).
        precision: Precision for the computation (e.g., float16).
        target_dir: Directory to save the results in.
    """
    dist.init_process_group(backend="nccl")
    rank = dist.get_rank()
    device_id = os.environ.get("LOCAL_RANK", rank)
    device = torch.device(f"cuda:{device_id}")
    torch.cuda.set_device(device)

    # Offset the start date so start_date is the first predicted tate.
    start_date = datetime.strptime(start_date, "%Y-%m-%d")
    start_date += timedelta(hours=start_hour)
    start_date -= timedelta(hours=assimilation_length)
    start_hour = int(start_date.hour)
    start_date = start_date.strftime("%Y-%m-%d")

    # Model and configs
    model_path = Path(model_path)
    latent_dir = model_path.parents[2]
    ae_cfg = compose(model_path.parents[4] / "config.yaml")
    denoiser_cfg = compose(model_path / "config.yaml")
    precision = getattr(torch, precision)
    use_bfloat16 = precision == torch.bfloat16
    trajectory_dt = denoiser_cfg.train.blanket_dt
    blanket_size = denoiser_cfg.train.blanket_size
    blanket_stride = blanket_size - blanket_overlap
    if diffusion.num_steps is None:
        diffusion_steps = denoiser_cfg.valid.denoising_steps
    else:
        diffusion_steps = diffusion.num_steps
    pressure_levels = (
        SUB_PRESSURE_LEVELS if ae_cfg.train.sub_pressure_levels else ERA5_PRESSURE_LEVELS
    )

    stats_path = latent_dir / "stats.pth"
    latent_stats = safe_load(stats_path)
    latent_mean = latent_stats["mean"]
    latent_std = latent_stats["std"]
    latent_std = torch.sqrt(latent_std**2 + ae_cfg.ae.noise_level**2)

    ae = load_auto_encoder(latent_dir / "ae", "model", device=device, eval_mode=True)
    ae.cuda()

    latent_shape = ae.latent_shape
    if len(latent_shape) == 3:
        latent_shape = latent_shape[0] * latent_shape[1], latent_shape[2]
    state_size = math.prod(latent_shape)
    latent_channels = latent_shape[-1]

    if use_bfloat16:
        torch.set_default_dtype(torch.bfloat16)

    noise_schedule = create_schedule(denoiser_cfg.train).to(device)
    backbone = load_denoiser(
        model_path, best=model_target == "best", overrides={"checkpointing": True}
    ).backbone.to(device)
    backbone.requires_grad_(False)

    if rank == 0:
        timestamps = create_trajectory_timestamps(
            start_date, start_hour, unpadded_trajectory_size, trajectory_dt
        )
        safe_save(
            timestamps[None],
            target_dir / "timestamps.pt",
        )
        del timestamps

        if not observe_full_states:
            create_masks(
                masks,
                assimilation_length,
                delay=0,
                split_seed=split_seed,
                save=target_dir,
                trajectory_dt=trajectory_dt,
            )

    timestamps = create_trajectory_timestamps(
        start_date, start_hour, padded_trajectory_size, trajectory_dt
    )[None]

    # Distribute blankets across GPUs.
    num_blankets = (padded_trajectory_size - blanket_size) // blanket_stride + 1
    num_gpus = dist.get_world_size()
    absolute_blanket_ids = torch.arange(num_blankets).tensor_split(num_gpus)
    absolute_blanket_ids = absolute_blanket_ids[rank]
    print(f"\n{rank} Blanket IDs: {absolute_blanket_ids} \n")

    rank_start_idx = absolute_blanket_ids[0] * blanket_stride
    rank_end_idx = absolute_blanket_ids[-1] * blanket_stride + blanket_size  # Not included

    print(f"Rank {rank} blanket indices: {rank_start_idx} -> {rank_end_idx}\n")

    denoiser = Denoiser(backbone).cuda()
    if use_bfloat16:
        denoiser = denoiser.to(torch.bfloat16)

    # True if at least one state is assimilated by this rank.
    rank_assimilates_obs = absolute_blanket_ids[0] * blanket_stride < assimilation_length

    if rank_assimilates_obs:
        local_assimilated_length = min(rank_end_idx, assimilation_length) - rank_start_idx
        slice_fn = lambda blanket_id: slice(
            0, max(0, assimilation_length - rank_start_idx - blanket_id * blanket_stride)
        )
        print(f"Rank {rank} assimilates {local_assimilated_length} states.\n")

        if observe_full_states:
            A = partial(
                observator_full,
                blanket_size=blanket_size,
                num_latent_channels=latent_channels,
                slice_fn=slice_fn,
            )
            latent_gt_ds = LatentBlanketDataset(
                path=latent_dir / latent_dump_name,
                start_date=start_date,
                start_hour=start_hour,
                end_date=DATASET_DATES_TEST[-1],
                blanket_size=blanket_size,
                standardize=True,
                stride=trajectory_dt,
            )

            # Build latent ground-truth full observations.
            y_obs_list = []
            cov_y_list = []
            cov_z = (ae_cfg.ae.noise_level / latent_std) ** 2

            with torch.no_grad():
                for blanket_id in range(len(absolute_blanket_ids)):
                    absolute_start_idx = rank_start_idx + blanket_id * blanket_stride

                    if absolute_start_idx >= assimilation_length:
                        y_obs_list.append(torch.empty(0).cuda())
                        cov_y_list.append(torch.empty(0).cuda())
                    else:
                        latent_gt, _ = latent_gt_ds[absolute_start_idx]
                        y_obs = latent_gt[: assimilation_length - absolute_start_idx]
                        cov_y = cov_z[None][None].expand(*y_obs.shape)

                        y_obs_list.append(y_obs.flatten().cuda())
                        cov_y_list.append(cov_y.flatten().cuda())
        else:
            (satellite_mask, sat_cov), ((stations_mask, _), stations_cov) = create_masks(
                masks,
                local_assimilated_length,
                delay=rank_start_idx,
                split_seed=split_seed,
                trajectory_dt=trajectory_dt,
            )

            stations_variables = (
                create_variable_mask(
                    torch.arange(
                        start=observed_variables.stations.low,
                        end=observed_variables.stations.high + 1,
                    )
                )
                if observed_variables.stations.enabled
                else None
            )
            satellite_variables = (
                create_variable_mask(
                    torch.arange(
                        start=observed_variables.satellites.low,
                        end=observed_variables.satellites.high + 1,
                    )
                )
                if observed_variables.satellites.enabled
                else None
            )
            mask_fn = partial(
                mask_state,
                stations_mask=stations_mask,
                stations_cov=stations_cov,
                satellite_mask=satellite_mask,
                satellite_cov=sat_cov,
                stations_variables=stations_variables,
                satellite_variables=satellite_variables,
            )

            # Build masked ground-truth observations
            variables_and_levels = {
                "state_variables": ERA5_VARIABLES,
                "context_variables": CONTEXT_VARIABLES,
                "levels": pressure_levels,
            }
            st = StandardizeTransform(PATH_STAT, **variables_and_levels)
            era5 = ERA5Dataset(
                path=PATH_ERA5,
                start_date=start_date,
                end_date=DATASET_DATES_TEST[-1],  # Could just compute the end date
                start_hour=start_hour,
                **variables_and_levels,
                transform=st,
                trajectory_dt=trajectory_dt,
            )

            y_obs_list = []
            cov_y_list = []

            context_obs_list = []
            timestamp_obs_list = []
            with torch.no_grad():
                for blanket_id in range(len(absolute_blanket_ids)):
                    absolute_start_idx = rank_start_idx + blanket_id * blanket_stride

                    y_obs = []
                    cov_y = []
                    c_obs = []
                    t_obs = []
                    for t in range(blanket_size):
                        if absolute_start_idx + t >= assimilation_length:
                            # Stop observing beyond the assimilation window
                            break

                        x_p, c_p, t_p = era5[absolute_start_idx + t]
                        x_p = rearrange(
                            x_p.to(device, non_blocking=True), "T Z Lat Lon -> T (Lat Lon) Z"
                        )
                        observed_values, cov_y_t = mask_fn(x_p, blanket_id * blanket_stride + t)
                        y_obs.append(observed_values)
                        cov_y.append(cov_y_t)

                        c_p = rearrange(
                            c_p.to(device, non_blocking=True), "T Z Lat Lon -> T (Lat Lon) Z"
                        )

                        c_obs.append(c_p)
                        t_obs.append(t_p)

                        del x_p

                    if len(y_obs) == 0:
                        y_obs_list.append(torch.empty(0).cuda())
                        cov_y_list.append(torch.empty(0).cuda())
                        context_obs_list.append(torch.empty(0).cuda())
                        timestamp_obs_list.append(torch.empty(0).cuda())
                    else:
                        y_obs_list.append(torch.cat(y_obs))
                        cov_y_list.append(torch.cat(cov_y))
                        context_obs_list.append(torch.cat(c_obs).cuda())
                        timestamp_obs_list.append(torch.cat(t_obs).cuda())

            context_fn = lambda blanket_id: context_obs_list[blanket_id]
            timestamp_fn = lambda blanket_id: timestamp_obs_list[blanket_id]

            A = partial(
                observator_partial,
                context_fn=context_fn,
                timestamp_fn=timestamp_fn,
                blanket_size=blanket_size,
                blanket_stride=blanket_stride,
                num_latent_channels=latent_channels,
                autoencoder=ae,
                latent_mean=latent_mean,
                latent_std=latent_std,
                slice_fn=slice_fn,
                mask_fn=mask_fn,
            )

            del st, era5

        denoiser = MMPSDenoiser(
            denoiser, A, y_obs_list, cov_y_list, iterations=diffusion.mmps_iters
        )

    denoiser = TrajectoryDenoiser(
        denoiser,
        blanket_size=blanket_size,
        blanket_stride=blanket_stride,
        state_size=state_size,
        distributed=True,
        pass_blanket_ids=rank_assimilates_obs,
        mode="causal",
    )

    denoiser = partial(denoiser, date=timestamps.cuda())

    @torch.no_grad()
    def sample():
        sampler = select_sampler(diffusion.sampler.type)

        sampler = sampler(
            denoiser=denoiser,
            steps=diffusion_steps,
            schedule=noise_schedule,
            silent=rank > 0,
            **diffusion.sampler.config,
        )
        x1 = torch.randn(len(timestamps), padded_trajectory_size * state_size).cuda()
        samp_start = (x1 * noise_schedule.sigma_tmax().cuda()).flatten(1).cuda()
        return sampler(samp_start).reshape((-1, padded_trajectory_size, *latent_shape)).cpu()

    if precision != torch.float16:
        sampled_traj = sample()
    else:
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            sampled_traj = sample()

    if rank == 0:
        # Trim to assimilation window.
        sampled_traj = sampled_traj[:, :unpadded_trajectory_size]
        safe_save(sampled_traj, target_dir / "trajectory.pt")

    dist.destroy_process_group()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("python forecast_aao.py [+id=<id>] model_path=... param1=A param2=B ...")
        return

    config = compose("configs/forecast_aao.yaml", overrides=sys.argv[1:])
    OmegaConf.set_readonly(config, False)
    OmegaConf.set_struct(config, False)

    if "id" in config:
        unique_id = config.pop("id")
    elif config.hardware.backend == "slurm":
        unique_id = wandb.util.generate_id()
    else:
        unique_id = os.environ["SLURM_JOB_ID"]  # even in async, should be in slurm job

    schedule_jobs(config, unique_id)


if __name__ == "__main__":
    main()
