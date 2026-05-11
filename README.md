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
# Build



 # Test Locally with Lambda Runtime Interface Emulator

# Pull the image from ECR (if you haven't already)
docker pull 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest

# Run the container locally on port 9000
docker run -p 9000:8080 032621928874.dkr.ecr.ca-central-1.amazonaws.com/fastapi-backend:latest




 # Clean the current image
aws ecr batch-delete-image --repository-name fastapi-backend --region ca-central-1  --image-ids imageTag=latest


aws ecr list-images --repository-name fastapi-backend --region ca-central-1  --query 'imageIds[*]' --output json | \
aws ecr batch-delete-image --repository-name fastapi-backend --region ca-central-1  --image-ids file:///dev/stdin