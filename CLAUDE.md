# Job Hunt Agents

## Environment

The following tools are installed in non-standard paths. Always prefix Bash commands with:
```
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
```

Or use the full paths below when invoking them:

- **Docker:** `/usr/local/bin/docker`
- **AWS CLI:** `/opt/homebrew/bin/aws`
- **AWS Account ID:** `052732928292`
- **AWS Region:** `us-east-1` — always pass `--region us-east-1` explicitly on every AWS CLI command. The CLI default region is `ap-southeast-4` and will silently create resources in the wrong region.

### Docker build command (all Lambda functions)
```
/usr/local/bin/docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --push \
  -t 052732928292.dkr.ecr.us-east-1.amazonaws.com/<repo-name>:latest \
  <function-dir>/
```

### Debugging Lambda issues
When a pipeline test fails or a Lambda behaves unexpectedly, **always check the Lambda logs first** before investigating pipes, state machines, or DynamoDB. Common issues are timeouts and cold start errors that are only visible in the logs.

```
aws logs describe-log-streams --log-group-name /aws/lambda/<function-name> \
  --region us-east-1 --order-by LastEventTime --descending \
  --query 'logStreams[0].logStreamName' --output text

aws logs get-log-events --log-group-name /aws/lambda/<function-name> \
  --log-stream-name <stream> --region us-east-1 \
  --query 'events[-20:].message' --output json
```

All AI Lambdas (job-summariser, company-researcher, cv-matcher, resume-tailor, cover-letter-generator) need **at least 120s timeout** — LLM API calls are slow. Default Lambda timeout is 3s and will silently time out.

### Running integration tests
```
source /Users/dom/Documents/Job_Hunt_Agents/lambda_venv/bin/activate
python3 tests/test_<name>.py
```

### ECR login (run before first push in a session)
```
/opt/homebrew/bin/aws ecr get-login-password --region us-east-1 \
  | /usr/local/bin/docker login --username AWS --password-stdin \
    052732928292.dkr.ecr.us-east-1.amazonaws.com
```
