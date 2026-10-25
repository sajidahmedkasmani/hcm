# Face fixtures (tests only)

| File | Source | Licence |
|---|---|---|
| `person_a.jpg` | NASA portrait of astronaut Eileen Collins, as shipped in scikit-image (`skimage.data.astronaut`), cropped | Public domain (NASA) |
| `person_a_eyes_closed.jpg` | `person_a.jpg` with the eyelids painted closed (synthetic blink frame) | Derived from public domain |
| `person_b.jpg` | Face region of `samples/data/messi5.jpg` from the OpenCV repository (used in OpenCV's tutorials) | OpenCV sample data |

They let the tests exercise real face detection and matching: same person under lighting/crop/rotation changes
(distance ~0.04–0.11), a different person (~0.83), two faces, no face, and a blink sequence.
