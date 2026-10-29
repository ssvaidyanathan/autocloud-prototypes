# Common Error Signatures Cookbook

## 1. HTTP 500 & Unhandled Exceptions
- **Signature**: `httpRequest.status = 500` or `jsonPayload.message =~ "Internal Server Error"`
- **Log pattern**:
  ```text
  resource.type = "cloud_run_revision"
  httpRequest.status >= 500
  ```

## 2. Out of Memory (OOM)
- **Signature**: Container memory limit exceeded.
- **Log pattern**:
  ```text
  resource.type = "cloud_run_revision"
  textPayload =~ ("Memory limit of" OR "terminated: out of memory" OR "OOMKilled")
  ```

## 3. Request Timeouts & Deadlines Exceeded
- **Signature**: `httpRequest.status = 504` or `DEADLINE_EXCEEDED`
- **Log pattern**:
  ```text
  resource.type = "cloud_run_revision"
  httpRequest.status = 504 OR textPayload =~ "context deadline exceeded"
  ```

## 4. Database Connection Failures
- **Signature**: `OperationalError: could not connect to server` or `connection pool exhausted`
- **Log pattern**:
  ```text
  textPayload =~ ("connection pool" OR "could not connect to server" OR "too many connections")
  ```
