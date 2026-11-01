"""Internal deployment protection; independent of coverage and session lifetime."""

# Cover the instructor's 1,800 second run plus setup/report overhead. Expiry only
# releases deployment protection: it never expires a session or deletes its data.
DEPLOYMENT_PROTECTION_SECONDS = 1900
DEPLOYMENT_RENEWAL_WINDOW_SECONDS = 1800
