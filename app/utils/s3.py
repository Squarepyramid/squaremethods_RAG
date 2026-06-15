"""
SquareMethods - S3 Utility
==========================
Place this file at: app/utils/s3.py

Handles image uploads to S3 for job aid procedure steps.
Images are stored publicly readable under the job-aids/ prefix.
"""

import os
import uuid
import logging
import boto3
from botocore.exceptions import ClientError

log    = logging.getLogger(__name__)
s3     = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ca-central-1"))
BUCKET = os.environ["S3_BUCKET_NAME"]


def upload_image(file_bytes: bytes, content_type: str, filename: str) -> str:
    """
    Upload an image to S3 and return its public URL.

    Key pattern: job-aids/images/{uuid}-{filename}
    The UUID prefix guarantees uniqueness even if two files share a name.
    """
    ext        = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    key        = f"job-aids/images/{uuid.uuid4()}-{filename}"

    try:
        s3.put_object(
            Bucket      = BUCKET,
            Key         = key,
            Body        = file_bytes,
            ContentType = content_type,
        )
    except ClientError as e:
        log.error(f"S3 upload failed: {e}")
        raise

    region = os.environ.get("AWS_REGION", "ca-central-1")
    url    = f"https://{BUCKET}.s3.{region}.amazonaws.com/{key}"
    log.info(f"Uploaded image to {url}")
    return url