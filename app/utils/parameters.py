import os
import boto3
from dotenv import load_dotenv



def get_param(name: str, region: str = "ca-central-1") -> str:
    try:
        ssm = boto3.client("ssm", region_name=region)

        response = ssm.get_parameter(
            Name=name,
            WithDecryption=True
        )

        return response["Parameter"]["Value"]

    except Exception as e:
        print(f"SSM ERROR fetching {name}: {str(e)}")
        raise