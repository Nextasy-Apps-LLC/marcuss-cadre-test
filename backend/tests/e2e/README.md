# e2e — the real thing at `BASE_URL`

These tests never import `app`; they speak HTTP to a running target, so they
are the only place the container, the Lambda Web Adapter and (later)
CloudFront are actually exercised. They are excluded from the default `pytest`
run by the `e2e` marker — turns cost real money once the model seams are live.

## Local: the real image in docker

```bash
docker build -t cadre-backend:local backend            # arm64 host, or add --platform
docker run --rm -p 8080:8080 \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_REGION=us-east-1 \
  cadre-backend:local

BASE_URL=http://localhost:8080 pytest -m e2e            # from backend/
```

Real AWS credentials belong in the container's environment, never in this
repo. Phase 1a makes no Bedrock call — the model seams are unimplemented — so
the suite passes without credentials today; Phase 1b's answered-turn cases
need them.

## Prod

```bash
BASE_URL=https://cadre.marcuss.pro pytest -m e2e
```

Against CloudFront every POST is signed for the Lambda Function URL's OAC, so
the client adds `x-amz-content-sha256` (KB-002); talking straight to the
container skips it. A curl-green run is not proof that streaming works
(KB-007) — watch tokens land in a browser tab before calling a change tested.
