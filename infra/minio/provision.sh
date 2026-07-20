#!/bin/sh
set -eu

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"
: "${MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY:?MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY is required}"
: "${MEMORIA_ARCHIVE_OBJECT_SECRET_KEY:?MEMORIA_ARCHIVE_OBJECT_SECRET_KEY is required}"
: "${MEMORIA_VOICE_OBJECT_ACCESS_KEY:?MEMORIA_VOICE_OBJECT_ACCESS_KEY is required}"
: "${MEMORIA_VOICE_OBJECT_SECRET_KEY:?MEMORIA_VOICE_OBJECT_SECRET_KEY is required}"

mc alias set local http://memoria-minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing local/memoria-archive
mc mb --ignore-existing local/memoria-voice
mc version enable local/memoria-archive
mc version enable local/memoria-voice

cat >/tmp/archive-policy.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketVersioning", "s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": ["arn:aws:s3:::memoria-archive"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": ["arn:aws:s3:::memoria-archive/*"]
    }
  ]
}
JSON

cat >/tmp/voice-policy.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketVersioning", "s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": ["arn:aws:s3:::memoria-voice"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": ["arn:aws:s3:::memoria-voice/*"]
    }
  ]
}
JSON

mc admin policy create local memoria-archive-rw /tmp/archive-policy.json
mc admin policy create local memoria-voice-rw /tmp/voice-policy.json
mc admin user add local "$MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY" "$MEMORIA_ARCHIVE_OBJECT_SECRET_KEY"
mc admin user add local "$MEMORIA_VOICE_OBJECT_ACCESS_KEY" "$MEMORIA_VOICE_OBJECT_SECRET_KEY"
mc admin policy attach local memoria-archive-rw --user "$MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY"
mc admin policy attach local memoria-voice-rw --user "$MEMORIA_VOICE_OBJECT_ACCESS_KEY"

mc stat local/memoria-archive >/dev/null
mc stat local/memoria-voice >/dev/null
