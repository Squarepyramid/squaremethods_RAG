# squaremethods_RAG
The RAG for squaremethods

Step 1: Create & activate a Python virtual environment (recommended)
# In your backend folder
python3 -m venv venv




# Activate it
source venv/bin/activate

# if you want to deactivate 
deactivate

# Install packages
pip install -r requirements.txt

# install uvicorn
pip install uvicorn

# Create the architeture
RAG_API_Skeleton/
│
├
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── services/
│   │   │   └── __init__.py
│   │   └── utils/
│   │       └── __init__.py
│   │
│   ├── requirements.txt
│   └── venv/


mkdir -p app/services
mkdir -p app/utils
touch app/__init__.py
touch app/services/__init__.py
touch app/utils/__init__.py
touch app/main.py

# run uvicorn
python3 -m uvicorn app.main:app --reload --port 8000

# install AWS CLI with:
sudo apt update
sudo apt install awscli -y

or
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
sudo apt install unzip -y
unzip awscliv2.zip
sudo ./aws/install

# verify the version
aws --version

# login to aws

aws configure

# test whether authentication works:
aws sts get-caller-identity

# Create repository for container
aws ecr create-repository --repository-name fastapi-backend

# downlaod docker and confirm version running
docker --version

# login to docker online
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 730xxxxxx.dkr.ecr.us-east-1.amazonaws.com

aws ecr get-login-password --region ca-central-1 | docker login --username AWS --password-stdin 032621928874.dkr.ecr.ca-central-1.amazonaws.com

# create Dockerfile


# Build and push:
docker build -t fastapi-backend .


# Docker tag
docker tag fastapi-backend:latest 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest
# Docker Push
docker push 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest
# Check the image architecture
docker inspect fastapi-backend | grep Architecture


# describe image
 aws ecr describe-images --repository-name fastapi-backend --region ca-central-1


# Complete build and push
# 1. Build with the correct tag name
docker buildx build --platform linux/amd64 \
  --output type=docker \
  --provenance=false \
  --sbom=false \
  -t fastapi-back-docker .


docker tag fastapi-back-docker:latest 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest


docker push 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest

aws lambda update-function-code \
  --function-name squaremethods_API \
  --image-uri 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest \
  --region ca-central-1



 # Test Locally with Lambda Runtime Interface Emulator

# Pull the image from ECR (if you haven't already)


# Login to ECR
aws ecr get-login-password --region ca-central-1 | \
  docker login --username AWS --password-stdin 032621928874.dkr.ecr.ca-central-1.amazonaws.com

# Pull and run locally on port 9000
docker run -p 9000:8080 \
  -e ROOT_PATH="" \
  -e AWS_ACCESS_KEY_ID=$(aws configure get aws_access_key_id) \
  -e AWS_SECRET_ACCESS_KEY=$(aws configure get aws_secret_access_key) \
  -e AWS_DEFAULT_REGION=ca-central-1 \
  032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest




  or 

docker run -p 9000:8080 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest



# test
curl -X POST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "2.0",
    "routeKey": "POST /chat",
    "rawPath": "/chat",
    "rawQueryString": "",
    "headers": {
      "content-type": "application/json",
      "accept": "application/json"
    },
    "requestContext": {
      "accountId": "123456789012",
      "apiId": "test",
      "domainName": "localhost",
      "http": {
        "method": "POST",
        "path": "/chat",
        "protocol": "HTTP/1.1",
        "sourceIp": "127.0.0.1",
        "userAgent": "curl"
      },
      "requestId": "test-request-id",
      "routeKey": "POST /chat",
      "stage": "$default"
    },
    "body": "{\"query\": \"your question here\", \"equipment_path\": \"your/equipment/path\", \"company_id\": \"your-company-id\", \"history\": []}",
    "isBase64Encoded": false
  }'

# Run locally using env
docker run --env-file .env -d  -p 8000:8000 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest
docker run --env-file .env -p 8000:8000 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest


# To see all containers
docker ps -a
# Stop all running containers
docker stop $(docker ps -q)
# Delete all containers
docker rm $(docker ps -aq)
# One-liner: stop + delete everything
docker stop $(docker ps -q) && docker rm $(docker ps -aq)

# delete all images
docker system prune -a

 # Clean the current image
aws ecr batch-delete-image --repository-name fastapi-backend --region ca-central-1  --image-ids imageTag=latest


aws ecr list-images --repository-name fastapi-backend --region ca-central-1  --query 'imageIds[*]' --output json | \
aws ecr batch-delete-image --repository-name fastapi-backend --region ca-central-1  --image-ids file:///dev/stdin