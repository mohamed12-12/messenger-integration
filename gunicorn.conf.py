# gunicorn.conf.py – Gunicorn configuration for AWS deployment
import multiprocessing

# Bind to all interfaces on port 5003
bind = "0.0.0.0:5003"

# Workers: 2 * CPU cores + 1 is the recommended formula
workers = multiprocessing.cpu_count() * 2 + 1

# Threads per worker (good for I/O-bound apps like this one)
threads = 2

# Request timeout in seconds
timeout = 60

# Graceful restart timeout
graceful_timeout = 30

# Keep-alive connections
keepalive = 5

# Log to stdout/stderr so AWS CloudWatch picks them up automatically
accesslog  = "-"
errorlog   = "-"
loglevel   = "info"

# Log format (includes request duration and status code)
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'
