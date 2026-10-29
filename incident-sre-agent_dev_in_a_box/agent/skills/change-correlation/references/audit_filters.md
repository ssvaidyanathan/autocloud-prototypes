# Cloud Audit Logs Filter Cookbook

## Admin Activity Log Name
```text
logName = "projects/<PROJECT_ID>/logs/cloudaudit.googleapis.com%2Factivity"
```

## Cloud Run Deployment Events
Look for Service updates and Revision creation:
```text
protoPayload.serviceName = "run.googleapis.com"
protoPayload.methodName =~ ("google.cloud.run.v1.Services.ReplaceService" OR "google.cloud.run.v2.Services.UpdateService")
```

## IAM Policy Changes
Detect service account and project IAM edits:
```text
protoPayload.methodName =~ ("SetIamPolicy" OR "CreateServiceAccountKey" OR "DeleteServiceAccountKey")
```

## Cloud SQL Configuration Modifications
```text
protoPayload.serviceName = "cloudsql.googleapis.com"
protoPayload.methodName =~ ("instances.update" OR "instances.restart" OR "instances.failover")
```
