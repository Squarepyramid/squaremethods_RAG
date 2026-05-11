# Lambda-compatible base image
FROM public.ecr.aws/lambda/python:3.11

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app ./app

# Fix permissions
RUN chmod -R 755 ./app

# ✅ CORRECT: Lambda handler format
CMD ["app.main.handler"]

# ❌ WRONG: Do NOT use these in Lambda
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
# CMD ["app.main:app"]