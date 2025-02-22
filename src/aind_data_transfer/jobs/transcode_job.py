import logging
import os.path
import subprocess
import sys
import time
from pathlib import Path
from shutil import copytree, ignore_patterns

from numcodecs import Blosc

from aind_data_transfer.config_loader.imaging_configuration_loader import (
    ImagingJobConfigurationLoader,
)
from aind_data_transfer.readers.imaging_readers import ImagingReaders
from aind_data_transfer.util.file_utils import is_cloud_url, parse_cloud_url
from aind_data_transfer.writers.imaging_writers import ExASPIMWriter

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


def _find_scripts_dir():
    scripts_dir = Path(os.path.abspath(__file__)).parents[3] / "scripts"
    if not scripts_dir.is_dir():
        raise Exception(f"scripts directory not found: {scripts_dir}")
    return scripts_dir


_SCRIPTS_DIR = _find_scripts_dir()

_S3_SCRIPT = _SCRIPTS_DIR / "s3_upload.py"
if not _S3_SCRIPT.is_file():
    raise Exception(f"script not found: {_S3_SCRIPT}")

_GCS_SCRIPT = _SCRIPTS_DIR / "gcs_upload.py"
if not _GCS_SCRIPT.is_file():
    raise Exception(f"script not found: {_GCS_SCRIPT}")

_OME_ZARR_SCRIPT = _SCRIPTS_DIR / "write_ome_zarr.py"
if not _OME_ZARR_SCRIPT.is_file():
    raise Exception(f"script not found: {_OME_ZARR_SCRIPT}")

_NG_LINK_SCRIPT = _SCRIPTS_DIR / "create_neuroglancer_links.py"

_SUBMIT_SCRIPT = _SCRIPTS_DIR / "cluster" / "submit.py"
if not _SUBMIT_SCRIPT.is_file():
    raise Exception(f"script not found: {_SUBMIT_SCRIPT}")


def _build_s3_cmd(
    data_src_dir: str,
    bucket: str,
    prefix: str,
    raw_image_dir_name: str,
    n_threads: int = 4,
) -> str:
    cmd = (
        f"python {_S3_SCRIPT} "
        f"--input={data_src_dir} "
        f"--bucket={bucket} "
        f"--s3_path={prefix} "
        f"--nthreads={n_threads} "
        f"--recursive "
        f"--exclude_dirs={raw_image_dir_name}"
    )
    return cmd


def _build_gcs_cmd(
    data_src_dir: str,
    bucket: str,
    prefix: str,
    raw_image_dir_name: str,
    n_threads: int = 4,
) -> str:
    cmd = (
        f"python {_GCS_SCRIPT} "
        f"--input={data_src_dir} "
        f"--bucket={bucket} "
        f"--gcs_path={prefix} "
        f"--nthreads={n_threads} "
        f"--recursive "
        f"--method=python "
        f"--exclude_dirs={raw_image_dir_name} "
    )
    return cmd


def _build_ome_zar_cmd(
    raw_image_dir: str, zarr_out: str, job_configs: dict
) -> str:
    compression_opts = _resolve_compression_options(job_configs)
    job_opts = job_configs["transcode_job"]
    job_cmd = (
        f"python {_OME_ZARR_SCRIPT} "
        f"--input={raw_image_dir} "
        f"--output={zarr_out} "
        f"--codec={compression_opts['cname']} "
        f"--clevel={compression_opts['clevel']} "
        f"--n_levels={job_opts['n_levels']} "
        f"--chunk_size={job_opts['chunk_size']} "
        f"--scale_factor=2 "
        f"--deployment=slurm"
    )
    if "chunk_shape" in job_opts and job_opts["chunk_shape"]:
        chunks = " ".join(str(el) for el in job_opts["chunk_shape"])
        job_cmd += f" --chunk_shape {chunks}"
    if "exclude" in job_opts and job_opts["exclude"]:
        exclusions = " ".join(job_opts["exclude"])
        job_cmd += f" --exclude {exclusions}"
    if "resume" in job_opts and job_opts["resume"]:
        job_cmd += " --resume"
    if "voxsize" in job_opts and job_opts["voxsize"] != "":
        voxsize = job_opts["voxsize"]
        job_cmd += f" --voxsize {voxsize}"
    return job_cmd


def _build_submit_cmd(
    job_cmd: str, job_configs: dict, wait: bool = False
) -> str:
    submit_args = job_configs["transcode_job"]["submit_args"]
    # FIXME: necessary to wrap job_cmd in quotes
    submit_cmd = f'python {_SUBMIT_SCRIPT} generate-and-launch-run --job_cmd="{job_cmd}"'
    for k, v in submit_args.items():
        submit_cmd += f" --{k}={v}"
    if wait:
        submit_cmd += " --wait"
    return submit_cmd


def _resolve_compression_options(job_configs: dict) -> dict:
    opts = {}

    try:
        compressor_kwargs = job_configs["transcode_job"]["compressor"][
            "kwargs"
        ]
    except KeyError:
        compressor_kwargs = {}

    opts["cname"] = compressor_kwargs.get("cname", "zstd")
    opts["clevel"] = compressor_kwargs.get("clevel", 1)
    opts["shuffle"] = compressor_kwargs.get("shuffle", Blosc.SHUFFLE)

    return opts


def main():
    job_configs = ImagingJobConfigurationLoader().load_configs(sys.argv[1:])

    data_src_dir = Path(job_configs["endpoints"]["raw_data_dir"])
    dest_data_dir = job_configs["endpoints"]["dest_data_dir"]
    if dest_data_dir.endswith("/"):
        # remove trailing slash
        dest_data_dir = dest_data_dir[:-1]

    reader = ImagingReaders.get_reader_name(data_src_dir)
    raw_image_dir = ImagingReaders.get_raw_data_dir(
        reader, data_src_dir
    )

    LOGGER.info(f"Transferring data to {dest_data_dir}")

    raw_image_dir_name = Path(raw_image_dir).name

    wait = job_configs["jobs"]["create_ng_link"]
    if wait:
        LOGGER.info(
            "Will wait for job to terminate before continuing execution"
        )

    zarr_out = dest_data_dir + "/" + raw_image_dir_name + ".zarr"
    if job_configs["jobs"]["transcode"]:
        job_cmd = _build_ome_zar_cmd(raw_image_dir, zarr_out, job_configs)
        submit_cmd = _build_submit_cmd(job_cmd, job_configs, wait)
        subprocess.run(submit_cmd, shell=True)
        LOGGER.info("Submitted transcode job to cluster")

    if job_configs["jobs"]["create_ng_link"]:
        ng_link_cmd = (
            f"python {_NG_LINK_SCRIPT} "
            f"--input={zarr_out} "
            f"--output={data_src_dir} "
            f"--vmin={job_configs['create_ng_link_job']['vmin']} "
            f"--vmax={job_configs['create_ng_link_job']['vmax']}"
        )
        subprocess.run(ng_link_cmd, shell=True)
        output_json = data_src_dir / "process_output.json"
        if not output_json.is_file():
            LOGGER.error(
                f"Creating neuroglancer link failed; {output_json} was not created"
            )

    if job_configs["jobs"]["create_metadata"]:
        if reader == ImagingReaders.Readers.exaspim.value:
            metadata_service_url = job_configs["endpoints"]["metadata_service_url"]
            writer = ExASPIMWriter(data_src_dir, metadata_service_url)
            writer.write_subject(data_src_dir)
            writer.write_data_description(data_src_dir)
            writer.write_procedures(data_src_dir)
        else:
            LOGGER.error(f"Fetching metadata not implemented for {reader}")

    if job_configs["jobs"]["upload_aux_files"]:
        LOGGER.info("Uploading auxiliary data")
        t0 = time.time()
        if is_cloud_url(dest_data_dir):
            provider, bucket, prefix = parse_cloud_url(dest_data_dir)
            if provider == "s3://":
                cmd = _build_s3_cmd(
                    data_src_dir, bucket, prefix, raw_image_dir_name
                )
            elif provider == "gs://":
                cmd = _build_gcs_cmd(
                    data_src_dir, bucket, prefix, raw_image_dir_name
                )
            else:
                raise Exception(f"Unsupported cloud storage: {provider}")
            subprocess.run(cmd, shell=True)
        else:
            copytree(
                data_src_dir,
                dest_data_dir,
                ignore=ignore_patterns(raw_image_dir_name),
            )
        LOGGER.info(
            f"Finished uploading auxiliary data, took {time.time() - t0}"
        )


if __name__ == "__main__":
    main()
