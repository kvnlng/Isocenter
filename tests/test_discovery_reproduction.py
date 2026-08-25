import pytest
from isocenter.discovery import ZoneDiscoverer

def test_discovery_too_many_zones_with_noise():
    """
    Reproduces the issue where discover_redaction_zones suggests too many zones
    because it doesn't filter out rare occurrences (noise).
    """

    # 1. Setup a scenario:
    # - A "real" text region appearing in many instances (slightly jittered but overlapping)
    # - Random noise appearing in few instances

    boxes = []

    # The "Real" Zone (appearing 10 times, overlapping)
    # Box format: [x, y, w, h]
    # Base: 100, 100, 50, 20
    for i in range(10):
        # Slight jitter +/- 2 pixels, ensures overlap
        boxes.append([100 + i%2, 100 + i%2, 50, 20])

    # The "Noise" Zones (appearing once each, scattered)
    noise_boxes = [
        [10, 10, 20, 10],   # Noise 1
        [500, 500, 30, 30], # Noise 2
        [300, 50, 10, 10],  # Noise 3
    ]
    boxes.extend(noise_boxes)

    # 2. Run merging logic with NEW padding
    # And we must replicate the filtering logic to verify it works (since _merge doesn't filter)

    # Simulate tagging (Real zones come from 5 different instances, noise from 1 each)
    tagged = []
    # 10 real boxes, let's say they come from 5 instances (2 boxes per instance, slightly jittered)
    for i in range(10):
        tagged.append( (boxes[i], f"inst_{i % 5}") )

    # Noise boxes come from unique instances
    start_id = 100
    for i in range(len(noise_boxes)):
         tagged.append( (noise_boxes[i], f"inst_{start_id + i}") )

    all_boxes = [t[0] for t in tagged]

    # Use NEW padding
    clusters = ZoneDiscoverer.group_boxes(all_boxes, padding=5)

    final_zones = []
    # Increase total instances to ensure noise (1 instance) < 10%
    # 5 noise-free instances + 5 real instances + 3 noise instances = 13
    # Actually, simpler: just set total to 20
    total_instances = 20
    min_occ = 0.1 # 10%

    for cluster in clusters:
        # Check source frequency
        unique_sources = {tagged[i][1] for i in cluster}
        if len(unique_sources) / total_instances < min_occ:
            continue # Filter noise

        cluster_boxes = [all_boxes[i] for i in cluster]
        merged = ZoneDiscoverer._union_box_list(cluster_boxes)

        if merged[2] > 5 and merged[3] > 5:
            final_zones.append(merged)

    # 3. Assertions
    print(f"\nResulting Zones: {len(final_zones)}")
    for z in final_zones:
        print(f" - {z}")

    assert len(final_zones) == 1, f"Expected 1 zone (main), but got {len(final_zones)}. Noise filtering failed."

def test_discovery_fragmentation():
    """
    Reproduces the issue where disjoint but adjacent text is not merged,
    creating multiple zones for what should be one.
    """
    # Scenario: Text "Patient Name" might be detected as two boxes "Patient" and "Name"
    # in one instance, and "Patient Name" in another.

    boxes = [
        [10, 10, 40, 20],   # "Patient"
        [55, 10, 40, 20],   # "Name" (Gap of 5 pixels from 50 to 55)
    ]

    # Use NEW padding (5 pixels)
    merged = ZoneDiscoverer._merge_overlapping_boxes(boxes, padding=5)

    # Should merge because gap is 5 pixels

    print(f"\nFragmentation Zones: {len(merged)}")

    assert len(merged) == 1, f"Expected 1 merged zone for adjacent text, got {len(merged)}."
