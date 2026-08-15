# Lambda-compatible base image
FROM public.ecr.aws/lambda/python:3.11

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj bin/micromamba && \
    MAMBA_ROOT_PREFIX=/opt/conda ./bin/micromamba create -y -r /opt/conda -n base -c conda-forge tesseract && \
    rm -rf ./bin /opt/conda/pkgs

ENV PATH="/opt/conda/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/conda/lib:${LD_LIBRARY_PATH}"
ENV TESSDATA_PREFIX="/opt/conda/share/tessdata"

# conda-forge's tesseract package doesn't reliably bundle language data --
# fetch English training data explicitly from tesseract's own official repo
# rather than assume it's already there.
RUN mkdir -p /opt/conda/share/tessdata && \
    curl -Ls -o /opt/conda/share/tessdata/eng.traineddata \
    https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata

RUN tesseract --version && tesseract --list-langs

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