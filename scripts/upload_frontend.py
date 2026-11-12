#!/usr/bin/env python3
"""Upload static SPA assets + config.json to private S3 bucket and invalidate CloudFront."""

import json
import mimetypes
import os
import sys
import time

import boto3


def main() -> None:
    if len(sys.argv) < 5:
        print(
            "Usage: upload_frontend.py <bucket> <cloudfront_url> <hosted_ui_url> <cf_dist_id>",
            file=sys.stderr,
        )
        sys.exit(1)

    bucket, cf_url, hosted_ui, dist_id = sys.argv[1:5]
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frontend_dir = os.path.join(project_root, "frontend")

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    for name in sorted(os.listdir(frontend_dir)):
        path = os.path.join(frontend_dir, name)
        if not os.path.isfile(path):
            continue
        mime, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            s3.put_object(
                Bucket=bucket,
                Key=name,
                Body=fh.read(),
                ContentType=mime or "application/octet-stream",
            )
        print(f"Uploaded s3://{bucket}/{name} ({mime or 'application/octet-stream'})")

    config_payload = {
        "mode": "cognito",
        "apiBase": cf_url,
        "hostedUiUrl": hosted_ui,
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "wafEnabled": True,
    }
    s3.put_object(
        Bucket=bucket,
        Key="config.json",
        Body=json.dumps(config_payload, indent=2).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-store",
    )
    print(f"Uploaded s3://{bucket}/config.json")

    cf = boto3.client("cloudfront", region_name="us-east-1")
    cf.create_invalidation(
        DistributionId=dist_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-{int(time.time())}",
        },
    )
    print(f"Created CloudFront invalidation for distribution {dist_id}")


if __name__ == "__main__":
    main()
