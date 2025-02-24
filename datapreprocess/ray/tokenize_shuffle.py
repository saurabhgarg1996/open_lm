import argparse
import collections
import hashlib
import io
import json
import os
import resource
import tarfile
import time
import traceback
from enum import Enum
from io import BytesIO
from typing import List
from loguru import logger
import pandas as pd

import boto3
import fsspec
import numpy as np
import psutil
import json
import webdataset as wds
from braceexpand import braceexpand
from transformers import GPTNeoXTokenizerFast

import ray
from ray._private.internal_api import memory_summary
from ray.data._internal.util import _check_pyarrow_version
from ray.data.block import Block, BlockMetadata
from ray.data.context import DataContext
from ray.data.datasource import Datasource, ReadTask
from ray.runtime_context import RuntimeContext
import pyarrow.fs as fs
import pyarrow.json

from tqdm import tqdm
from io import BytesIO


class SpecialTokens(Enum):
    END_OF_TEXT = 0
    PAD = -1
    END_OF_DOCUMENT = -2


class PadType(Enum):
    CIRCULAR = 0
    PAD_TOKEN = 1


def download_to_memory_with_progress(bucket_name, key):
    s3 = boto3.client("s3")

    # Use the head_object call to get the total size of the object
    meta_data = s3.head_object(Bucket=bucket_name, Key=key)
    total_size = int(meta_data.get("ContentLength", 0))

    # Create a progress bar with tqdm
    progress = tqdm(total=total_size, unit="B", unit_scale=True)

    # Callback to update the progress bar
    def progress_callback(bytes_transferred):
        progress.update(bytes_transferred)

    # Use BytesIO to store the downloaded bytes in memory
    buffer = BytesIO()
    s3.download_fileobj(bucket_name, key, buffer, Callback=progress_callback)

    progress.close()

    # Reset the buffer's position to the beginning
    buffer.seek(0)

    # Return the in-memory buffer
    return buffer.read()


def parse_s3_path(s3_path):
    """
    Extract the bucket and key from an S3 path.

    Args:
    - s3_path (str): The S3 path in the format "s3://bucket/key"

    Returns:
    - tuple: A tuple containing the bucket and key
    """
    if not s3_path.startswith("s3://"):
        raise ValueError("Invalid S3 path format. Must start with 's3://'")

    s3_parts = s3_path[5:].split("/", 1)
    bucket = s3_parts[0]

    if len(s3_parts) > 1:
        key = s3_parts[1]
    else:
        key = ""
    return bucket, key


def dl_parse_s3(data, creds=None):
    worker_id = ray.get_runtime_context().get_worker_id()
    if creds is not None:
        client = boto3.client(
            "s3",
            aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=creds["AWS_SESSION_TOKEN"],
        )
    else:
        client = boto3.client("s3")
    out_dicts = []
    for path in data["path"]:
        bucket, key = parse_s3_path(path)
        json_lines = download_to_memory_with_progress(bucket, key).decode().splitlines()
        jsons = [json.loads(x) for x in json_lines]
        out_dicts += jsons
    return pd.DataFrame(out_dicts)


def dist_tokenize(data, tokenizer, content_key):
    out_dicts = []
    for tokens in data[content_key]:
        tokens = tokenizer(tokens) + [SpecialTokens.END_OF_TEXT.value]
        out_dict = {}
        out_dict["tokens"] = tokens
        out_dicts.append(out_dict)
    return pd.DataFrame(out_dicts)


def cut_to_context(jsonl_batch, seqlen=1024, pad_type=PadType.CIRCULAR):
    tokens_list = jsonl_batch["tokens"]
    flat_token_list = [item for sublist in tokens_list for item in sublist]
    repartioned_lists = [flat_token_list[i : i + seqlen] for i in range(0, len(flat_token_list), seqlen)]
    end_len = len(repartioned_lists[-1])
    if len(repartioned_lists[-1]) < seqlen:
        if pad_type == PadType.CIRCULAR:
            repartioned_lists[-1] = repartioned_lists[-1] + repartioned_lists[0][: (seqlen - end_len)]
        else:
            repartioned_lists[-1] = repartioned_lists[-1] + [SpecialTokens.PAD.value] * (seqlen - end_len)
    return {"tokens": repartioned_lists}


def add_hash(item, column="tokens"):
    item["hash"] = hash(str(item[column]))
    return item


def map_write_wds(batch, batch_size, folder, counter):
    # Calculate tar index based on the first id

    # Determine the number of leading zeros dynamically based on total_count
    tar_index = ray.get(counter.increment.remote())

    digits = 8  # default to 8
    # Format tar index with the determined number of leading zeros
    tar_index_str = f"{tar_index:0{digits}}"

    # Create tar file name
    tar_name = f"{tar_index_str}.tar"

    # Write the batch to a tarball using webdataset's TarWriter
    bio = io.BytesIO()
    with wds.TarWriter(bio) as sink:
        for i in range(len(batch["tokens"])):
            tokens = [int(x) for x in batch["tokens"][i]]
            uid = hashlib.md5(json.dumps(tokens).encode()).hexdigest()
            sample = {"__key__": uid, "json": tokens}
            sink.write(sample)

    bio.seek(0)
    write_to_location(folder, tar_name, bio)
    return batch


def write_to_location(folder, tar_name, bio):
    path = f"{folder}/{tar_name}"

    # Check if the path is an S3 path
    if path.startswith("s3://"):
        s3 = boto3.client("s3")

        # Properly extract bucket and key from the S3 path
        s3_path_parts = path[5:].split("/")
        bucket = s3_path_parts[0]
        key = "/".join(s3_path_parts[1:])

        try:
            s3.put_object(Bucket=bucket, Key=key, Body=bio.getvalue())
        except Exception as e:
            assert False, f"bucket is {bucket} key is {key} and {e}"

    else:
        # Create directory if it doesn't exist
        if not os.path.exists(folder):
            os.makedirs(folder)

        try:
            with open(path, "wb") as f:
                f.write(bio.getvalue())
        except Exception as e:
            assert False, f"error is {path} and {e}"


def load_tokenizer(tokenizer):
    if tokenizer == "EleutherAI/gpt-neox-20b":
        enc = GPTNeoXTokenizerFast.from_pretrained("EleutherAI/gpt-neox-20b")
        return lambda x: enc(x).input_ids
    else:
        raise ValueError(f"Unknown Tokenizer: {tokenizer}")


def glob_files(path, suffix=".jsonl"):
    """
    Glob files based on a given path and suffix.
    Supports both local and S3 paths.

    :param path: path to glob. Can be local or S3 (e.g., s3://bucket-name/path/)
    :param suffix: suffix of files to match. Defaults to ".jsonl"
    :return: list of file paths matching the pattern
    """
    if path.startswith("s3://"):
        # Use boto3 for S3 paths
        s3 = boto3.client("s3")
        bucket_name, prefix = path[5:].split("/", 1)

        # Ensure the prefix ends with a '/'
        if not prefix.endswith("/"):
            prefix += "/"

        # List the objects in the bucket with the given prefix
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
        all_files = [f"s3://{bucket_name}/{obj['Key']}" for objects in pages for obj in objects.get("Contents", [])]

        # Filter out the files based on the suffix
        matching_files = [f for f in all_files if f.endswith(suffix)]

    else:
        # Use glob for local paths
        search_pattern = f"{path.rstrip('/')}/*{suffix}"
        matching_files = glob.glob(search_pattern)

    return matching_files


def get_filesystem(environment):
    """
    Create a pyarrow.fs.FileSystem based on provided AWS credentials.

    :param environment: Dictionary containing AWS credentials.
    :return: pyarrow.fs.S3FileSystem
    """
    # Extract the AWS credentials from the environment dictionary
    access_key = environment.get("AWS_ACCESS_KEY_ID")
    secret_key = environment.get("AWS_SECRET_ACCESS_KEY")
    session_token = environment.get("AWS_SESSION_TOKEN", None)  # Session token might be optional

    # Create and return the S3FileSystem
    return fs.S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        region="us-west-2",
    )


@ray.remote
class GlobalCounter:
    def __init__(self):
        self.value = 0

    def increment(self):
        self.value += 1
        return self.value

    def get_counter(self):
        return self.value


def human_to_bytes(s):
    """
    Convert human-readable byte size strings to actual number of bytes.

    Supports:
        B: bytes
        KB, kB, Kb, kB: kilobytes
        MB, mB, Mb, mB: megabytes
        GB, gB, Gb, gB: gigabytes
        TB, tB, Tb, tB: terabytes
        PB, pB, Pb, pB: petabytes

    Example:
        human_to_bytes('5.2 GB') -> 5.2 * 1024^3
    """

    symbols = ("B", "K", "M", "G", "T", "P")
    letter = s[-2:].strip().upper() if s[-2:].strip().upper()[:-1] in symbols else s[-1:].upper()
    number = float(s[: -len(letter)].strip())

    if letter == "B":
        return int(number)
    elif "K" in letter:
        return int(number * 1024)
    elif "M" in letter:
        return int(number * 1024**2)
    elif "G" in letter:
        return int(number * 1024**3)
    elif "T" in letter:
        return int(number * 1024**4)
    elif "P" in letter:
        return int(number * 1024**5)
    else:
        raise ValueError(f"Unsupported format: {s}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="input path", type=str, required=True)
    parser.add_argument(
        "--output",
        help="output path",
        type=str,
        required=True
        # e.g s3://dcnlp-data/rpj_tokenized_upsampled_eleutherai_deduplicated/
    )
    parser.add_argument("--content_key", type=str, default="text")
    parser.add_argument("--no_shuffle", help="do not dedup + random shuffle", action="store_true")
    parser.add_argument("--seqlen", type=int, default=2048)
    parser.add_argument("--pad_type", type=str, default="circular")
    parser.add_argument("--tokenizer", type=str, default="EleutherAI/gpt-neox-20b")
    parser.add_argument("--initial_batch_size", type=int, default=2048)
    parser.add_argument("--wds_chunk_size", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--per_node_parallelism", type=int, default=8)
    parser.add_argument("--subset", type=int, default=None)
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--ray_address", type=str, default=None)
    parser.add_argument("--block_size", type=str, default="10MB")
    parser.add_argument("--ray_spill_location", type=str, default="s3://dcnlp-hub/ray_spill")

    args = parser.parse_args()
    # configure remote spilling
    creds = {k: v for k, v in os.environ.items() if k.startswith("AWS")}
    runtime_env = {"env_vars": creds}
    block_size = human_to_bytes(args.block_size)

    if "AWS_ACCESS_KEY_ID" in creds:
        fs = get_filesystem(creds)
    else:
        fs = None
    if args.ray_address is None:
        ray.init(runtime_env=runtime_env)
    else:
        ray.init(args.ray_address, runtime_env=runtime_env)
    num_nodes = len(ray.nodes())
    # TODO  support multiple inputs
    input_paths = glob_files(args.input, suffix=".jsonl")
    if args.subset is not None:
        input_paths = input_paths[: args.subset]
    print(f"num files ={len(input_paths)}")
    num_files = len(input_paths)
    num_cores = os.cpu_count()
    output_path = args.output
    seqlen = args.seqlen + 1
    cores_to_use = args.per_node_parallelism
    batch_size = args.initial_batch_size
    wds_chunk_size = args.wds_chunk_size
    content_key = args.content_key
    if args.pad_type == "circular":
        pad_type = PadType.CIRCULAR
    elif args.pad_type == "pad_token":
        pad_type = PadType.PAD_TOKEN
    else:
        raise ValueError(f"Unknown pad_type = {args.pad_type}")

    ctx = DataContext.get_current()
    ctx.use_push_based_shuffle = True
    ctx.execution_options.resource_limits.object_store_memory = float("inf")
    ray.data.DataContext.get_current().execution_options.verbose_progress = True
    start_time = time.time()
    tokenizer = load_tokenizer(args.tokenizer)
    logger.info(f"Total number of keys = {len(input_paths)}")
    df = pd.DataFrame(input_paths, columns=["path"])
    # TODO fix hack for now.
    ds = ds.map_batches(
        dl_parse_s3,
        batch_size=1,
        fn_kwargs={"creds": creds},
        batch_format="pandas",
        num_cpus=16,
    )
    ds = ds.map_batches(
        dist_tokenize,
        batch_size=batch_size,
        fn_kwargs={"tokenizer": tokenizer, "content_key": content_key},
        batch_format="pandas",
        num_cpus=16,
    )
    ds = ds.map_batches(
        cut_to_context,
        batch_size=batch_size,
        fn_kwargs={"pad_type": pad_type, "seqlen": seqlen},
        batch_format="pandas",
    )
    ds = ds.map(add_hash)
    tokenize_context_end = time.time()
    # sorting by hash is a random shuffle
    ds = ds.sort(key="hash")
    if args.materialize:
        ds = ds.materialize()
    counter = GlobalCounter.remote()
    ds = ds.map_batches(
        map_write_wds,
        batch_size=wds_chunk_size,
        fn_kwargs={
            "batch_size": wds_chunk_size,
            "folder": args.output.strip("/"),
            "counter": counter,
        },
    ).count()
    end_time = time.time()
    duration = end_time - start_time
    print("Tokenize + Shuffle script Finished in", duration)
    print("==== Driver memory summary ====")
    maxrss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1e3)
    print(f"max: {maxrss / 1e9}/GB")
    process = psutil.Process(os.getpid())
    rss = int(process.memory_info().rss)
    print(f"rss: {rss / 1e9}/GB")
    try:
        print(memory_summary(stats_only=True))
    except Exception:
        print("Failed to retrieve memory summary")
        print(traceback.format_exc())
    print("")
