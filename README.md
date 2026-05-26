# OutboundAI

OutboundAI is a Docker-deployed outbound voice calling platform built around LiveKit, Gemini Live, Twilio SIP trunking, Supabase CRM, and Cal.com scheduling.

## Deployment mode

This repository is now configured for env-only runtime configuration:
- The VPS or Coolify environment variables are the only source of truth.
- `.env` is not loaded at runtime.
- `.env` is excluded from Docker build context.
- Dashboard settings writes are intentionally disabled.

## Hostinger VPS + Coolify

1. Create a new application in Coolify from this repository.
2. Keep the detected `Dockerfile`.
3. Set the public port to `8000`.
4. Add a health check path of `/health`.
5. Add all required environment variables from `.env.example` into Coolify.
6. Deploy.

## Required environment variables

These are required for startup:
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `GOOGLE_API_KEY`
- `TWILIO_TRUNK_SID`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `CALCOM_API_KEY`
- `CALCOM_BOOKING_URL`
- `CALCOM_TIMEZONE`

Optional but commonly needed:
- `SYSTEM_PROMPT`
- `ENABLED_TOOLS`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`
- `DEFAULT_TRANSFER_NUMBER`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_ENDPOINT_URL`
- `S3_REGION`
- `S3_BUCKET`
- `DEEPGRAM_API_KEY`

## Startup behavior

- `start.sh` validates required environment variables before the app starts.
- FastAPI is served on port `8000`.
- `/health` returns HTTP 200 for container health checks.
- If required environment variables are missing, the container exits immediately with a clear error.

## Supabase

Run `supabase_schema.sql` once in your Supabase SQL editor before first production use.

## Cal.com scheduling

Cal.com is the primary scheduling engine:
- real availability is checked from Cal.com
- bookings are created in Cal.com first
- bookings are then mirrored into Supabase for CRM and reporting

If Cal.com fails, the app logs the error and falls back to CRM-only booking.

## Notes

- The dashboard still displays config values, but edits are blocked because runtime configuration is env-managed.
- Agent profiles, campaigns, CRM data, call logs, and appointments remain stored in Supabase.
