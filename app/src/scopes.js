// OAuth scopes the app asks for (atproto.com/specs/permission). Granular:
// create and delete org.consentvid.videoV2 / consentV2 records, and upload
// video blobs (for "Upload from my computer"), nothing else.
// web/app/client-metadata.json must list every scope requested
// (test/vectors.test.js checks it).
export const SCOPE =
  'atproto repo:org.consentvid.videoV2?action=create&action=delete repo:org.consentvid.consentV2?action=create&action=delete blob:video/*'

// For a PDS that doesn't support granular scopes yet: the app-password-
// equivalent transitional scope. The app still only writes org.consentvid.*
// records; the user is told before it is requested.
export const FALLBACK_SCOPE = 'atproto transition:generic'
