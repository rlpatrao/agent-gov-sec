# Container image for the governance chokepoint Lambdas (LLM proxy / data proxy /
# A2A broker). The single-file zip is no longer sufficient: the handlers import
# the shared enforcement library (galaxy_gov/shared + galaxy_gov/remote) and the
# agent_os/agent_sre/agentmesh toolkit, so the deployment is a container image.
#
# The image bakes the resolved policy registry at /var/task/agent-controls.json
# (produced by `python -m galaxy_gov.policy_export`); the function points
# GOV_POLICY_REGISTRY_PATH at it. Pick the handler per function via the CMD
# override in Terraform (image_config.command).
#
# Build context = repo root:
#   docker build -f cloud_adapters/aws/infra/lambda/Dockerfile -t <ecr>/galaxy-gov-proxy .
FROM public.ecr.aws/lambda/python:3.12

# Toolkit + governance runtime deps (agent_os_kernel, agent_sre, agentmesh-platform,
# pydantic, numpy, …). requirements-proxy.txt is the chokepoint-only subset.
COPY cloud_adapters/aws/infra/lambda/requirements-proxy.txt ./
RUN pip install --no-cache-dir -r requirements-proxy.txt

# The enforcement library (shared + remote + adapters) and the handlers. Note we
# copy galaxy_gov/shared, galaxy_gov/remote, galaxy_gov/adapters and core/ — NOT
# payload_agents (the chokepoints never import the agent codebase).
COPY galaxy_gov/ ${LAMBDA_TASK_ROOT}/galaxy_gov/
COPY core/ ${LAMBDA_TASK_ROOT}/core/
COPY cloud_adapters/aws/infra/lambda/bedrock_proxy.py ${LAMBDA_TASK_ROOT}/
COPY cloud_adapters/aws/infra/lambda/data_proxy.py ${LAMBDA_TASK_ROOT}/
COPY cloud_adapters/aws/infra/lambda/a2a_broker.py ${LAMBDA_TASK_ROOT}/
# AgentCore Gateway interceptor handlers (request/response content controls).
COPY cloud_adapters/aws/agentcore/request_interceptor.py ${LAMBDA_TASK_ROOT}/
COPY cloud_adapters/aws/agentcore/response_interceptor.py ${LAMBDA_TASK_ROOT}/

# Bake the resolved registry artifact (override at build with --build-arg if desired).
COPY cloud_adapters/aws/infra/lambda/agent-controls.json ${LAMBDA_TASK_ROOT}/agent-controls.json
ENV GOV_POLICY_REGISTRY_PATH=/var/task/agent-controls.json

# Default handler; Terraform overrides per function via image_config.command.
CMD ["bedrock_proxy.handler"]
