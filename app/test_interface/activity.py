"""Internal deployment protection; independent of coverage and session lifetime."""

# 2,400 second Phase 5 suite plus setup, transport and report overhead.
# Expiry releases protection only; it never expires a session or deletes data.
DEPLOYMENT_PROTECTION_SECONDS = 2700
DEPLOYMENT_RENEWAL_WINDOW_SECONDS = 2400
