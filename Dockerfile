# One image for every Lambda function; template.yaml picks the handler per function with
# ImageConfig.Command. A container image because numpy + pandas + faiss + google-genai exceed the
# 250 MB limit of zip packages. No data/ in the image: the index lives on S3 (D11), secrets in SSM.
# Ported from v1 Dockerfile (73b6fda).
FROM public.ecr.aws/lambda/python:3.12

COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt

COPY core ${LAMBDA_TASK_ROOT}/core
COPY jobs ${LAMBDA_TASK_ROOT}/jobs
COPY aws_lambda ${LAMBDA_TASK_ROOT}/aws_lambda

CMD ["aws_lambda.api.handler"]
