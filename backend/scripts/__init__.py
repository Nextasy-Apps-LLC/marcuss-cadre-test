"""Operational scripts. Not packaged into the runtime image — these run in
CI and on a laptop, against a real AWS account, and the container has no
business carrying them (or boto3) into a cold start."""
