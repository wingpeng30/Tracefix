"""Fixed names shared by policy metadata and the versioned job control protocol."""

JOB_PROTOCOL_VERSION = 1
JOB_CONTROL_CAPABILITIES = (
    "job_start_control", "job_status_control", "job_read_control",
    "job_cancel_control", "job_shutdown_control",
)
