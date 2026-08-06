# arm64 container Lambda running FastAPI under the AWS Lambda Web Adapter.
#
# The adapter turns the Lambda invoke into an ordinary HTTP request against
# uvicorn on :8080, so the same image runs unchanged locally
# (`docker run -p 8080:8080`) and in Lambda.
#
# AWS_LWA_INVOKE_MODE=response_stream is the whole reason this stack streams.
# In buffered mode the adapter waits for a complete body before returning and
# every SSE event arrives at once at the end. It only takes effect behind a
# RESPONSE_STREAM Function URL — an API Gateway HTTP API in front would
# re-buffer regardless of this setting.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 AS adapter
FROM public.ecr.aws/lambda/python:3.13-arm64

COPY --from=adapter /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_PORT=8080 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

WORKDIR /var/task

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
