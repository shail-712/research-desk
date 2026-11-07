"""
AWS Lambda entry points (one container image; template.yaml picks the handler per function):

    aws_lambda.api.handler          API Gateway HTTP API (watchlist, quotes, news, feed, ask jobs)
    aws_lambda.ask_worker.handler   async worker for /ask (Quick or Deep)
    aws_lambda.scan.handler         EventBridge Scheduler (price alerts)
    aws_lambda.notify.handler       DynamoDB Streams (new FEED items -> Telegram)

Each handler module calls runtime.bootstrap() at import time, i.e. once per cold start: JSON
logs, /tmp paths, secrets from SSM. All logic lives in core/ (SPEC §7).
"""
