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


# expose with Lambda Function URL 
aws lambda create-function-url-config \
  --function-name squaremethods_API \
  --auth-type NONE \
  --region ca-central-1


  https://dyxguxncdvevbjssa6jnoszhgu0qeduy.lambda-url.ca-central-1.on.aws/


  #  Create trigger 
  Option 1: Quick Setup via AWS Console

Go to API Gateway Console
Create API → REST API → Build
API Name: fastapi-backend-api
Create API
Create Resource:

Actions → Create Resource
Resource Name: {proxy+}
Resource Path: {proxy+}
✅ Enable API Gateway CORS
Create Resource

Create Method:

Select the {proxy+} resource
Actions → Create Method → ANY
Integration type: Lambda Function Proxy
Lambda Function: squaremethods_API
✅ Use Lambda Proxy integration
Save


Deploy API:

Actions → Deploy API
Deployment stage: [New Stage]
Stage name: prod
Deploy



# Get ACM certificates
aws acm request-certificate \
  --domain-name chatapi.squaremethods.com \
  --validation-method DNS \
  --region ca-central-1

  # Now get the DNS validation record to add to Route 53:
  aws acm describe-certificate \
  --certificate-arn arn:aws:acm:ca-central-1:032621928874:certificate/31d8d927-a4e9-4d88-8bfb-09cdd3c450ea \
  --region ca-central-1 \
  --query "Certificate.DomainValidationOptions"

# Add the validation record to Route 53:
  aws route53 change-resource-record-sets \
  --hosted-zone-id Z0328387Z4P8H93AL7U0 \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "_4dde17a94d8aa77b944368b80c43a04c.chatapi.squaremethods.com.",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{
          "Value": "_f3d13c1e90cf2f0645d6695123b32fa1.jkddzztszm.acm-validations.aws."
        }]
      }
    }]
  }'

  # ACM to validate. Check status with:
  aws acm describe-certificate \
  --certificate-arn arn:aws:acm:ca-central-1:032621928874:certificate/31d8d927-a4e9-4d88-8bfb-09cdd3c450ea \
  --region ca-central-1 \
  --query "Certificate.Status"

  # Now create the custom domain in API Gateway:
  aws apigateway create-domain-name \
  --domain-name chatapi.squaremethods.com \
  --regional-certificate-arn arn:aws:acm:ca-central-1:032621928874:certificate/31d8d927-a4e9-4d88-8bfb-09cdd3c450ea \
  --endpoint-configuration types=REGIONAL \
  --region ca-central-1

  # Now map the API to the custom domain:
  aws apigateway create-base-path-mapping \
  --domain-name chatapi.squaremethods.com \
  --rest-api-id hkbyrp82t3 \
  --stage prod \
  --region ca-central-1

  # update route 53
  aws route53 change-resource-record-sets \
  --hosted-zone-id Z0328387Z4P8H93AL7U0 \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "chatapi.squaremethods.com.",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{
          "Value": "d-a757r0vu5g.execute-api.ca-central-1.amazonaws.com"
        }]
      }
    }]
  }'

  # test
  curl -X POST "https://chatapi.squaremethods.com/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "who are you",
    "equipment_path": "string",
    "company_id": "string",
    "history": []
  }'




  # TIME FOR PG VECTOR

  # connect to postgress
  aws lambda update-function-configuration \
  --function-name squaremethods_API \
  --environment Variables="{
    DB_HOST=squaremethods-db.czqyiw6okqqm.ca-central-1.rds.amazonaws.com,
    DB_NAME=squaremethods,
    DB_USER=squaremethods_db,
    DB_PASSWORD=Ighohor1245$,
    DB_PORT=5432
  }" \
  --region ca-central-1



# Configure postgresql client first
sudo apt-get update && sudo apt-get install -y postgresql-client

  # Connect to the DB: remember to whitelist ip in SG
  psql -h squaremethods-db.czqyiw6okqqm.ca-central-1.rds.amazonaws.com \
     -U squaremethods_db \
     -d squaremethods \
     -p 5432

  # create chat tables SQL. 
  CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL,
    user_id UUID NOT NULL,
    equipment_id UUID,
    equipment_path VARCHAR(500) NOT NULL,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON UPDATE CASCADE ON DELETE CASCADE,
    company_id UUID NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ON chat_sessions (company_id, user_id);
CREATE INDEX ON chat_sessions (equipment_id);
CREATE INDEX ON chat_sessions (updated_at DESC);
CREATE INDEX ON chat_messages (session_id);
CREATE INDEX ON chat_messages (created_at);


# add the Bedrock VPC endpoint so Lambda can reach it from inside the VPC:

aws ec2 create-vpc-endpoint \
  --vpc-id vpc-05d2d65721cf3c21d \
  --service-name com.amazonaws.ca-central-1.bedrock-runtime \
  --vpc-endpoint-type Interface \
  --subnet-ids subnet-0f78be033838f2c86 subnet-0fc9c6f774e509a0a subnet-0da0720515b563661 \
  --security-group-ids sg-0b15b019cd1968cd4 \
  --private-dns-enabled \
  --region ca-central-1


  # Rebuild abd deploy
  docker buildx build --platform linux/amd64 \
  --no-cache \
  --output type=docker \
  --provenance=false \
  --sbom=false \
  -t fastapi-back-docker . && \
aws ecr get-login-password --region ca-central-1 | \
  docker login --username AWS --password-stdin 032621928874.dkr.ecr.ca-central-1.amazonaws.com && \
docker tag fastapi-back-docker:latest 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest && \
docker push 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest && \
aws lambda update-function-code \
  --function-name squaremethods_API \
  --image-uri 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest \
  --region ca-central-1

  