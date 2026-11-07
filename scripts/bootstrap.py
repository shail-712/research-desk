"""
Setup around `sam deploy` (run from the project root; uses no Gemini quota):

    python -m scripts.bootstrap --skip index watchlist   # BEFORE the first deploy: secrets only, no stack needed
    python -m scripts.bootstrap --skip secrets           # after the deploy: index + watchlist
    python -m scripts.bootstrap                      # secrets + index (create-only) + watchlist
    python -m scripts.bootstrap --replace            # replace the S3 index after `cli.py ingest`
    python -m scripts.bootstrap --overwrite-secrets  # after changing a value in .env
    python -m scripts.bootstrap --skip index         # (secrets | index | watchlist)

1. Secrets: every secret set in .env (SECRET_NAMES) becomes an SSM SecureString under
   /research-desk/. Values are never printed, only names and created/kept/missing.
2. Index: data/index/index.npz -> s3://<DataBucket>/vectors/index.npz. Create-only
   (If-None-Match: *): an existing index is kept. `--replace` swaps exactly the version it read
   (If-Match on its ETag), so two writers can't overwrite each other. The header check refuses an
   index built with another embedding model (D11). No embedding calls: the index is uploaded as built.
3. Watchlist: NVDA, AMD, INTC, only if the table is empty.

Table and bucket names come from the stack outputs, read only when the index or watchlist step runs.
Secrets go first because each Lambda container reads SSM once at cold start: a container started
before the secrets exist keeps answering 503 until it is recycled. Adapted from v1 scripts/aws_bootstrap.py
(73b6fda): v2 secret names (+ FINNHUB_API_KEY), the v2 .npz index with ETag replace; the
embedding-cache push/pull is cut (D13).
"""

import argparse
import sys

from core import config
from core.config import env, setup_logging

SECRET_NAMES = ("GEMINI_API_KEY", "FINNHUB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "SEC_USER_AGENT", "APP_ACCESS_CODE")
INDEX_KEY = "vectors/index.npz"


def stack_outputs(cfn, stack_name: str) -> dict:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def put_secrets(ssm, path: str, overwrite: bool = False) -> dict[str, str]:
    """Returns {name: "created" | "updated" | "kept ..." | "missing in .env"}. Never returns values."""
    from botocore.exceptions import ClientError

    result = {}
    for name in SECRET_NAMES:
        value = env(name)
        if not value:
            result[name] = "missing in .env"
            continue
        try:
            ssm.put_parameter(Name=f"{path.rstrip('/')}/{name}", Value=value, Type="SecureString",
                              Overwrite=overwrite)
            result[name] = "updated" if overwrite else "created"
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterAlreadyExists":
                raise
            result[name] = "kept (use --overwrite-secrets to replace)"
    return result


def upload_index(s3, bucket: str, replace: bool = False, path=None, key: str = INDEX_KEY) -> str:
    """Uploads the local index. Create-only unless `replace`."""
    from botocore.exceptions import ClientError

    from core.vectors import INDEX_FILE, IndexData, S3Index, VectorIndexError

    path = path or config.INDEX_DIR / INDEX_FILE
    if not path.exists():
        return f"no local index at {path} (run `python cli.py ingest NVDA AMD INTC` first)"
    npz = path.read_bytes()
    rows = len(IndexData.from_npz(npz))
    target = S3Index(bucket, key, client=s3)
    current = None
    if replace:
        try:
            current = s3.head_object(Bucket=bucket, Key=key)["ETag"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
    try:
        etag = target.upload(npz, replace_etag=current)
    except VectorIndexError as e:
        if "already exists or changed" in str(e):
            return ("already exists (kept; use --replace after a new ingest)" if not replace
                    else "changed on S3 while uploading; not overwritten, re-run --replace")
        raise
    return f"{'replaced' if current else 'uploaded'} {rows} chunks to s3://{bucket}/{key} (ETag {etag})"


def seed_watchlist(table_name: str, region: str, resource=None) -> str:
    from core.store import DEFAULT_WATCHLIST
    from core.store_dynamo import DynamoStore

    store = DynamoStore(table_name, region, resource=resource)
    before = len(store.list_watchlist())
    store.seed_watchlist(DEFAULT_WATCHLIST)
    return "seeded NVDA, AMD, INTC" if before == 0 else f"kept the existing watchlist ({before} tickers)"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack-name", default="research-desk")
    parser.add_argument("--region", default=env("AWS_REGION", "ap-southeast-2"))
    parser.add_argument("--ssm-path", default="/research-desk")
    parser.add_argument("--overwrite-secrets", action="store_true", help="replace existing SSM secrets")
    parser.add_argument("--replace", action="store_true", help="replace the S3 index (If-Match on its ETag)")
    parser.add_argument("--skip", nargs="*", default=[], choices=["secrets", "index", "watchlist"])
    args = parser.parse_args(argv)
    setup_logging("WARNING")

    import boto3

    session = boto3.Session(region_name=args.region)
    print(f"Region {args.region}, SSM path {args.ssm_path}")
    if "secrets" not in args.skip:
        for name, status in put_secrets(session.client("ssm"), args.ssm_path, args.overwrite_secrets).items():
            print(f"  secret {name}: {status}")
    if {"index", "watchlist"} <= set(args.skip):
        return 0
    outputs = stack_outputs(session.client("cloudformation"), args.stack_name)
    print(f"Stack {args.stack_name}: API {outputs.get('ApiUrl')}")
    if "index" not in args.skip:
        print("  index:", upload_index(session.client("s3"), outputs["DataBucketName"], args.replace))
    if "watchlist" not in args.skip:
        print("  watchlist:", seed_watchlist(outputs["TableName"], args.region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
