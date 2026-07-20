# V2 Digital Signage

## Purpose and exposure

Digital Signage centrally manages browser-based image rotations for independently named TVs. It is additive to the existing portal, has no location model, does not change V1, and remains hidden unless `digital_signage_v2` is enabled globally or for a named principal through the existing V2 exposure settings.

Management capabilities are:

- `digital_signage.view`
- `digital_signage.manage_groups`
- `digital_signage.manage_media`
- `digital_signage.manage_displays`
- `digital_signage.reset_display_credentials`

These default to administrators and managers. Store and lead accounts receive no signage access by default.

## Private R2 storage

Images are stored in a private S3-compatible Cloudflare R2 bucket. The intended bucket is `erupted-media`, but the application never hardcodes a bucket, account, endpoint, or credential. The immutable object key is `digital-signage/images/{sha256}`.

Required configuration:

```text
R2_ENDPOINT_URL
R2_BUCKET_NAME
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_REGION=auto
DIGITAL_SIGNAGE_MAX_UPLOAD_BYTES=15728640
```

Use credentials scoped only to the Erupted media bucket. The application fails closed on upload when durable storage is unavailable and never falls back automatically to local disk. Tests use the explicit in-memory adapter.

The bucket remains private. Browser media is delivered through authenticated application proxy routes with a content-hash ETag, conditional responses, `private` caching, and `nosniff`.

## TV creation and kiosk login

Create a TV under **Digital Signage → TV Displays**. Each TV has a unique case-insensitive name, URL slug, username, and Argon2-hashed password. A generated or supplied plaintext password is shown only after creation or reset and is never stored or audited.

The stable kiosk URL is:

```text
/display/{slug}
```

Open that URL in the TV browser, sign in with that TV’s credentials, then leave the page open in fullscreen or kiosk mode. Display sessions use a separate cookie and database table from employee sessions. Disabling a TV or resetting its password revokes its active display sessions. A display account cannot access management routes or request another TV’s playlist.

At most nine enabled, unarchived TVs are allowed. This is a configurable application rule protected by a PostgreSQL transaction advisory lock; it is not a schema limit.

## Advertisement-group workflow

1. Create a group and name it.
2. Select one or more TVs. **Select all current active TVs** selects only TVs that exist now; future TVs do not inherit the group.
3. Choose the start date, optional real end date, optional daily window, priority, and enabled state.
4. Upload images in the Media Library or choose existing reusable media.
5. Add media items, set 5–300 second durations or mark one active item Permanent, and save their explicit order.
6. Preview the fitted artwork in the group editor.

`end_date = NULL` is forever. Daily start and end must both be supplied, and end must be later than start. Equal and overnight windows are rejected; use date scheduling when a campaign must cross midnight.

Groups and TVs use normalized assignment rows. Media is independent from assignments. Duplicating a group copies group/item/assignment records while reusing the same media assets and storage objects.

## Uploads and reuse

The accessible upload control supports drag-and-drop and click-to-choose through one normal file input and one server endpoint. Accepted formats are decoded JPEG, PNG, and WebP. Validation covers byte limit, actual format, extension/MIME agreement, successful decoding, filename safety, and dimensions. Artwork is not cropped, stretched, or recompressed. Non-16:9 images are accepted with a warning and rendered using `object-fit: contain`.

SHA-256 deduplication uses `(media_type, content_hash)`. Uploading the same binary again reuses its asset and immutable object. Referenced media cannot be archived. Archiving an unused asset retains the private object for conservative later cleanup.

HTML and ZIP uploads are deliberately rejected with **“HTML animation packages are not enabled yet.”** `HTML_ANIMATION` remains reserved in the data model. Safe HTML animations require a later isolated-origin, archive-validation, sandbox, and CSP security milestone.

## Playback, Permanent, and temporary offline operation

The authenticated TV requests one effective playlist derived from its session identity. Eligible groups are enabled, assigned, in their date range, inside their optional daily window, and not archived. Groups sort by descending priority and deterministic creation/ID ties; items sort by explicit order and deterministic creation/ID ties.

One enabled permanent item is allowed per group. When several assigned groups contain permanent content, the first permanent item in the highest-priority deterministic group ordering wins. Permanent content ignores duration but the browser continues polling.

The player:

- preloads upcoming images and crossfades layers without white flashes;
- rotates locally without a server request per slide;
- refreshes playlist metadata about every 300 seconds;
- uses browser caching and conditional requests;
- keeps showing the last valid content when refresh fails;
- stores only non-secret playlist metadata in local storage;
- shows an Erupted fallback before any valid content has loaded.

This is temporary best-effort offline behavior, not a service-worker offline application.

## Known limitations and future seams

This milestone does not provide video, audio, HTML packages, locations, inventory rules, analytics, device control, health monitoring, split screens, native TV apps, or kiosk OS provisioning. Media types, explicit displays, normalized group assignments, and the centralized playlist service preserve seams for those later features without adding speculative tables.
