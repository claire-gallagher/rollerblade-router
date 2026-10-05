#!/usr/bin/env bash
# One-time GCP setup that the Cloud Run "Connect repository" flow doesn't do:
#   - a Cloud Storage bucket holding data/ (too big for GitHub), to mount into the service
#   - the two API keys in Secret Manager
#   - access for the service's account to both
# Run from the repo root after `uv run scripts/prep_data.py` and `gcloud auth login`:
#   bash scripts/setup_gcp.sh
# Re-run it after rebuilding data/ to upload the new files.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-east1}"  # use the same region as the Cloud Run service
BUCKET="${BUCKET:-${PROJECT}-skate-data}"

set -a; source <(tr -d '\r' < .env); set +a
: "${STADIA_API_KEY:?missing in .env}" "${NYC_GEOCLIENT_KEY:?missing in .env}"
[ -f data/loops.json ] || { echo "data/ is not built: run uv run scripts/prep_data.py"; exit 1; }

gcloud services enable storage.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com --project "$PROJECT"

# Cloud Run runs as the default compute service account unless you pick another one.
SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

echo "== Uploading data/ to gs://$BUCKET"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 ||
  gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access
gcloud storage rsync data "gs://$BUCKET" --exclude '.*\.staging\.tif$'
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" --role roles/storage.objectViewer >/dev/null

echo "== Storing API keys in Secret Manager"
put_secret() {  # name, value: create the secret or add a new version, and let the service read it
  if gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT" >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic --project "$PROJECT" >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor --project "$PROJECT" >/dev/null
}
put_secret stadia-api-key "$STADIA_API_KEY"
put_secret nyc-geoclient-key "$NYC_GEOCLIENT_KEY"

echo "== Letting the service call Gemini on Vertex AI"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/aiplatform.user --condition=None >/dev/null

echo
echo "Done. In the Cloud Run service, set:"
echo "  Volume:   Cloud Storage bucket $BUCKET, read-only, mounted at /data"
echo "  Env var:  DATA_DIR=/data"
echo "  Secrets:  STADIA_API_KEY -> stadia-api-key (latest), NYC_GEOCLIENT_KEY -> nyc-geoclient-key (latest)"
