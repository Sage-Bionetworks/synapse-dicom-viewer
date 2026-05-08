
You said: So synapse stores individual files as “entities”.
So synapse stores individual files as “entities”. We can make range requests on the pre-signed AWS urls that we can get do them. Dicom images concise of dozens to hundreds of linked dcm files. Would we be able to couple up a dicom viewer to a synapse folder containing all the structured dcm entities

10:00 AM
Claude responded: Yes, this is very doable, and the pattern you're describing is essentially what NCI's Imaging Data Commons does — they call it "static DICOMweb.

Architected DICOMweb integration strategy leveraging Synapse's S3 infrastructure
Architected DICOMweb integration strategy leveraging Synapse's S3 infrastructure
Yes, this is very doable, and the pattern you're describing is essentially what NCI's Imaging Data Commons does — they call it "static DICOMweb." The trick is bridging the gap between what DICOM viewers expect (a DICOMweb-compliant server speaking QIDO-RS / WADO-RS) and what you have (a folder of opaque entities accessible via pre-signed S3 URLs that happen to support range reads).

The practical architecture has two parts. First, you need DICOM metadata extracted up front — at minimum StudyInstanceUID, SeriesInstanceUID, SOPInstanceUID, modality, instance number — either stored as Synapse annotations on each .dcm entity or compiled into per-series JSON manifests. Without this you'd be range-reading headers from hundreds of files just to figure out which slices belong together, which is too slow at viewer load time. Second, you need a thin shim that implements the DICOMweb REST surface but resolves requests against Synapse: QIDO queries become Synapse entity queries against those annotations, and WADO retrievals turn into pre-signed URL fetches (or redirects). OHIF Viewer or Cornerstone.js then plug in unmodified. The shim can be a tiny Lambda or even pre-generated static JSON if your collections are stable.

The range-request piece is genuinely useful once you're past discovery — viewers can pull just the pixel data offsets they need for the current slice, scroll lazily through a series, or grab thumbnails without fetching full frames. The real gotchas are CORS on the S3 bucket (pre-signed URLs need the viewer's origin allowed), auth handoff (the shim has to broker the Synapse credentials so the browser never sees them), and request fan-out for series with hundreds of instances — HTTP/2 multiplexing helps but you'll want to think about prefetch strategy. Worth looking at RadicalImaging's static-wado tooling and the IDC codebase before building anything from scratch; they've solved the exact pattern and it's MIT-licensed.

You said: Assuming dcm files are organised by folders we do have that structure in Synapse.
Assuming dcm files are organised by folders we do have that structure in Synapse. But not in the underlying s3 bucket where the objects are stored

10:03 AM
Claude responded: That's fine and actually doesn't change much — the folder hierarchy only needs to exist somewhere queryable, and Synapse's entity tree is queryable via the RES…

Reconciled Synapse's abstraction layer with S3's flat storage model
Reconciled Synapse's abstraction layer with S3's flat storage model
That's fine and actually doesn't change much — the folder hierarchy only needs to exist somewhere queryable, and Synapse's entity tree is queryable via the REST API. The S3 layout being flat (with synthetic keys like <projectId>/<uuid>/...) is opaque to the viewer pipeline either way, because the shim resolves entities → pre-signed URLs through Synapse, not through S3 prefix listings.

Where this matters in practice: your QIDO layer becomes "walk the Synapse folder via getChildren (or query an entity view) and return entities matching study/series filters" rather than "list S3 objects under a prefix." Entity views are probably the right primitive here — create one scoped to your DICOM collection with the relevant DICOM tags exposed as annotation columns, and you get SQL-style queries (WHERE StudyInstanceUID = ...) that map cleanly onto QIDO-RS semantics. The shim translates QIDO query params → Synapse table query → list of entity IDs → pre-signed URLs. WADO retrieval then just fetches those URLs with range headers.

The one real consequence of the flat S3 layout is that you can't use off-the-shelf static DICOMweb generators that output a tree of .json and .dcm files into a bucket and then point a viewer at the bucket directly (the IDC pattern). Those tools assume they own the key structure. So you're committed to running the shim — you can't go fully static — but the shim is small (Lambda or a tiny FastAPI service) and gives you the auth integration as a bonus, since browser-side OHIF never sees a Synapse token; the shim brokers it.

If the folders in Synapse already roughly map to studies (one folder per study, sub-folders or just files per series), enumeration is essentially free. If they don't, the entity view approach sidesteps the folder structure entirely and lets the DICOM tags themselves define grouping.

You said: What is our best option for a the viewer front end
What is our best option for a the viewer front end

10:07 AM
Claude responded: OHIF Viewer is almost certainly the right call, and it's not a close decision for the architecture you've described.

Weighed viewer options against architecture requirements and use case
Weighed viewer options against architecture requirements and use case
OHIF Viewer is almost certainly the right call, and it's not a close decision for the architecture you've described. It speaks DICOMweb natively (so it'll plug straight into your shim with zero protocol translation), it's the viewer that NCI's IDC ships, which means you're walking a path that's already been load-tested against the exact "Synapse-style federated storage + thin DICOMweb facade" pattern, and it's extensible enough that you can add HTAN-specific overlays — segmentation masks, spatial transcriptomics ROIs, pathology annotations — as plugins rather than forks. It's React-based, embeddable in a portal, and has a working plugin model for measurement tools, segmentation, MPR, and 3D volume rendering. The feature set is generous to the point of being slightly heavy, but you can strip modes you don't need.

The main alternative worth considering is going one layer down to Cornerstone3D (the rendering engine OHIF is built on) and assembling your own minimal viewer. This makes sense only if you want very tight integration into a bespoke HTAN portal where OHIF's full UI shell would feel bolted-on, or if you want a stripped-back embedded preview rather than a full clinical-grade workstation. The cost is real engineering work to rebuild things OHIF gives you for free — hanging protocols, study browser, tool palettes, keyboard shortcuts — so I'd default to OHIF unless there's a specific UX reason not to.

One caveat worth flagging given HTAN's data mix: if any of those DCM folders are actually DICOM-WSI (whole slide pathology), traditional radiology viewers handle them poorly. OHIF has a microscopy extension, but the purpose-built option there is Slim (also from the IDC/MGH crowd, also DICOMweb-native, so the same shim works). A reasonable end state is OHIF for radiology modalities and Slim for pathology, both pointing at the same backend — which is essentially what IDC does. Worth checking which modalities are actually in scope before committing to a single viewer.

You said: Ideally we would have something we could say iframe into a synapse entity wiki page to embed the viewer in the synapse ui
Ideally we would have something we could say iframe into a synapse entity wiki page to embed the viewer in the synapse ui

10:09 AM
Claude responded: The good news is OHIF is built for this — it's a static React SPA, so you host it once (S3 + CloudFront is the standard pattern), pass StudyInstanceUIDs plus a…

Architected OHIF embedding strategy with authentication and Synapse integration considerations
Architected OHIF embedding strategy with authentication and Synapse integration considerations
The good news is OHIF is built for this — it's a static React SPA, so you host it once (S3 + CloudFront is the standard pattern), pass StudyInstanceUIDs plus a data-source identifier as URL params, and the iframe widget in the wiki points at it. End-to-end you'd get something like ${iframe?site=https://htan-viewer.sagebionetworks.org/viewer?dataSources=synapse&studyUIDs=...&folder=syn123456}, with the Synapse entity ID acting as the handoff between the wiki context and the viewer's data resolution. The shim, OHIF build, and viewer config all live outside Synapse; the wiki only needs to know the URL.

The genuinely awkward part is auth, and it forces a real design choice. For open-tier HTAN data this is trivial — the shim mints pre-signed URLs that work for anyone, and the iframe is fully self-contained. For controlled-access data there's no clean built-in mechanism today, and you've got three options roughly in order of pleasantness: (a) Synapse OAuth from inside the iframe, where the user clicks "authorize" once and the viewer holds a token — works but adds friction inside what should feel seamless; (b) postMessage handoff from the parent Synapse page passing a short-lived token to the iframe — cleanest UX but requires SWC-side cooperation, which the current sandboxed iframe widget probably won't allow without changes; (c) the wiki renderer itself interpolates a short-lived token into the iframe src server-side, which is probably the most practical interim path and is small enough to pitch as an SWC enhancement (a "Synapse-aware iframe widget" that knows to inject a scoped access token).

Two practical caveats worth flagging now. First, Synapse's iframe widget is typically sandboxed, and OHIF leans on localStorage for viewer state (hanging protocols, measurements, layout) — you'll want to check which sandbox flags are set and possibly request allow-same-origin and allow-scripts at minimum, or accept that viewer state won't persist between visits. Second, this is a generic enough pattern — embed-an-external-viewer-with-context — that I'd be tempted to propose it as a proper first-class Synapse widget rather than living forever as a wiki iframe hack. If you make the case to the SWC team now, the DICOM viewer becomes the wedge use case for a category that would also serve molecular viewers, IGV.js, Vitessce, and whatever else HTAN ends up needing.






Claude is AI and can make mistakes. Please double-check responses.

