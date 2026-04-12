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

### Lambda function names
- `job-scraper` — Apify scrape trigger
- `job-processor` — SQS consumer, sequential orchestrator (MaxConcurrency=2)
- `job-summariser` — extracts structured summary from job description
- `company-researcher` — web search + company fit scoring
- `cv-matcher` — scores CV against job summary
- `resume-tailor` — on-demand resume tailoring
- `cover-letter-generator` — on-demand cover letter generation
- `api` — API Gateway handler (jobs list, auth, on-demand triggers)

### Infrastructure
- **SQS queue:** `job-processing-queue` (`https://sqs.us-east-1.amazonaws.com/052732928292/job-processing-queue`)
- **DynamoDB tables:** `jobs`, `companies`, `candidate_profiles`
- **EventBridge Pipe:** DynamoDB stream → SQS (INSERT events only)

### Debugging Lambda issues
When a pipeline test fails or a Lambda behaves unexpectedly, **always check the Lambda logs first** before investigating pipes, state machines, or DynamoDB. Common issues are timeouts and cold start errors that are only visible in the logs.

One-liner to fetch the latest log stream and last 30 events:
```bash
STREAM=$(/opt/homebrew/bin/aws logs describe-log-streams \
  --log-group-name /aws/lambda/<function-name> \
  --region us-east-1 --order-by LastEventTime --descending \
  --query 'logStreams[0].logStreamName' --output text) && \
/opt/homebrew/bin/aws logs get-log-events \
  --log-group-name /aws/lambda/<function-name> \
  --log-stream-name "$STREAM" --region us-east-1 \
  --query 'events[*].message' --output json
```

To check the most recent N streams (useful when multiple executions ran in parallel):
```bash
/opt/homebrew/bin/aws logs describe-log-streams \
  --log-group-name /aws/lambda/<function-name> \
  --region us-east-1 --order-by LastEventTime --descending \
  --query 'logStreams[:3].{name: logStreamName, last: lastEventTimestamp}' \
  --output json
```

All AI Lambdas (job-summariser, company-researcher, cv-matcher, resume-tailor, cover-letter-generator) need **at least 120s timeout** — LLM API calls are slow. Default Lambda timeout is 3s and will silently time out.

### Clearing tables between test runs
Always purge the SQS queue when clearing tables — otherwise in-flight messages will re-insert ghost items.
```bash
# Clear jobs table
/opt/homebrew/bin/aws dynamodb scan --table-name jobs --region us-east-1 \
  --query 'Items[*].id.S' --output json | \
python3 -c "
import json, sys, subprocess
ids = json.load(sys.stdin)
for id in ids:
    subprocess.run(['/opt/homebrew/bin/aws','dynamodb','delete-item','--table-name','jobs','--region','us-east-1','--key',json.dumps({'id':{'S':id}})],check=True)
print(f'Deleted {len(ids)} jobs')
"

# Clear companies table
/opt/homebrew/bin/aws dynamodb scan --table-name companies --region us-east-1 \
  --query 'Items[*].company_name.S' --output json | \
python3 -c "
import json, sys, subprocess
names = json.load(sys.stdin)
for name in names:
    subprocess.run(['/opt/homebrew/bin/aws','dynamodb','delete-item','--table-name','companies','--region','us-east-1','--key',json.dumps({'company_name':{'S':name}})],check=True)
print(f'Deleted {len(names)} companies')
"

# Purge SQS queue
/opt/homebrew/bin/aws sqs purge-queue \
  --queue-url "https://sqs.us-east-1.amazonaws.com/052732928292/job-processing-queue" \
  --region us-east-1
```
Note: `purge-queue` can only be called once every 60 seconds. Wait ~30s after purging before scraping to ensure in-flight messages have drained.

### Architecture decision log
`ARCHITECTURE.md` in the repo root is a running log of every significant design and implementation decision made on this project. **Keep it up to date.** When a new decision is made — a bug fix that reveals a design flaw, a refactor, a new component, a change to an existing approach — add an entry under the relevant section using the format already established in the file:

```
### [Post-build] — Short title
**What:** What was built or changed
**Why:** The reasoning, including what problem it solves
**Alternatives considered:** Other approaches that were evaluated
```

Use the tag prefix that best describes when the decision was made:
- `[Pre-build]` — decided before any code was written
- `[Build]` — decided during the initial build phase
- `[Post-build]` — decided after the initial build (fixes, refactors, architectural changes)

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

---

## CI/CD

### GitHub Actions deploy pipeline
`.github/workflows/deploy.yml` — added 2026-04-12 via cloud session.

**What it does:** On push to `main`, detects which Lambda function directories changed and rebuilds/deploys only those functions. Uses OIDC auth (no stored AWS credentials), Docker Buildx with GHA layer cache, then calls `aws lambda update-function-code` to complete the deployment.

**Status: workflow file is committed and pushed, but the AWS IAM setup is not yet done.**

### ACTION REQUIRED — one-time AWS setup before the pipeline will work

1. **Create the OIDC provider** (once per AWS account):
```bash
/opt/homebrew/bin/aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --region us-east-1
```

2. **Create IAM role** named `github-actions-lambda-deploy` with this trust policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::052732928292:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike": { "token.actions.githubusercontent.com:sub": "repo:lildome/job-hunt-agents:*" }
    }
  }]
}
```

3. **Attach this permissions policy** to the role:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage", "ecr:PutImage",
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"
      ],
      "Resource": "arn:aws:ecr:us-east-1:052732928292:repository/*"
    },
    {
      "Effect": "Allow",
      "Action": ["lambda:UpdateFunctionCode"],
      "Resource": "arn:aws:lambda:us-east-1:052732928292:function:*"
    }
  ]
}
```

Once the role exists, any push to `main` that touches a function directory will trigger an automatic build and deploy. No GitHub secrets needed.
