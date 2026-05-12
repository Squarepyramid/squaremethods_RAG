import boto3
import json

def get_embedding(text: str) -> list:
    client = boto3.client("bedrock-runtime", region_name="ca-central-1")
    
    body = json.dumps({
        "inputText": text[:8000]  # Titan V2 max input
    })
    
    response = client.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        contentType="application/json",
        accept="application/json",
        body=body
    )
    
    result = json.loads(response["body"].read())
    return result["embedding"]