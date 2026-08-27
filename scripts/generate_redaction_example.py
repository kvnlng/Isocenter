import os
import sys
import random
import datetime
import numpy as np

# Ensure we can import isocenter
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from isocenter.builders import DicomBuilder
from isocenter.io_handlers import DicomExporter
try:
    from PIL import Image, ImageDraw, ImageFont
    import faker
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

def generate_ct_pixels(rows, cols, frames, text, text_pos):
    """
    Generates CT-like pixel data (16-bit) with burned-in text at a specific location.
    Includes random shapes for compression testing.
    """
    frame_list = []
    
    # Font setup
    try:
        # Try to find a font
        font_paths = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Arial.ttf"
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, 24)
                break
        if not font:
            font = ImageFont.load_default()
    except:
        font = ImageFont.load_default()

    for f in range(frames):
        # 1. Create base image (L mode = 8-bit, we scale later)
        img = Image.new("L", (cols, rows), color=10)
        draw = ImageDraw.Draw(img)
        
        # 2. Draw Random Shapes (Compression Noise)
        # Deterministic per frame for consistency
        rnd = random.Random(f) 
        for _ in range(5):
            x1, x2 = sorted([rnd.randint(0, cols), rnd.randint(0, cols)])
            y1, y2 = sorted([rnd.randint(0, rows), rnd.randint(0, rows)])
            draw.ellipse((x1, y1, x2, y2), outline=100 + rnd.randint(0, 50), width=2)
            
            x1, x2 = sorted([rnd.randint(0, cols), rnd.randint(0, cols)])
            y1, y2 = sorted([rnd.randint(0, rows), rnd.randint(0, rows)])
            draw.rectangle((x1, y1, x2, y2), outline=50 + rnd.randint(0, 50), width=2)

        # 3. Burn in Text at CONSISTENT location
        # Simulate patient banner or overlay
        x, y = text_pos
        # Draw explicit box background for the text to make it "burned in" clearly
        text_bbox = draw.textbbox((x, y), text, font=font)
        draw.rectangle(text_bbox, fill=255) # White bg
        draw.text((x, y), text, fill=0, font=font) # Black text
        
        # 4. Text that moves (e.g. scrolling slice info) - Optional
        draw.text((10, rows - 30), f"Slice {f+1}/{frames}", fill=200, font=font)

        # 5. Convert to numpy & Scale to CT Range (12-bit usually)
        arr = np.array(img).astype(np.uint16)
        arr = arr * 16 # Scale 0-255 to 0-4080 range (approx 12-bit)
        
        frame_list.append(arr)

    if frames > 1:
        return np.stack(frame_list, axis=0) # (Frames, Rows, Cols)
    return frame_list[0]

def main(output_dir="test_data/redaction_examples"):
    if not HAS_DEPS:
        print("Requires 'pillow' and 'faker'. pip install pillow faker")
        sys.exit(1)
    fake = faker.Faker()
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # Scenarios: (Manufacturer, Model, Text, (x, y))
    scenarios = [
        ("GE", "Revolution CT", "PH: Hospital A", (20, 20)),                # Top-Left
        ("Siemens", "Somatom Force", "[ confidential ]", (400, 450)),     # Bottom-Rightish
        ("Philips", "Brilliance 64", "Pt: John Doe", (200, 10))            # Left-Middle
    ]
    
    print(f"Generating redaction examples in {output_dir}...")
    
    for man, model, secret_text, pos in scenarios:
        # Generate 2 patients per machine
        for _ in range(2):
            pat_name = f"{fake.last_name()}^{fake.first_name()}"
            pat_id = f"PID-{fake.numerify('#####')}"
            
            print(f" -> {man} {model}: {pat_name}")
            
            # Setup Metadata
            study_uid = f"1.2.840.999.{random.randint(10000,99999)}"
            series_uid = f"{study_uid}.1"
            
            # Create Pixels (Multi-frame CT)
            # CT usually 512x512
            rows, cols = 512, 512
            frames = 5 # Small multiframe for test
            
            # We explicitly burn in the PATIENT NAME as well as the 'secret_text'
            burn_in = f"{secret_text} | {pat_name}"
            pixels = generate_ct_pixels(rows, cols, frames, burn_in, pos)
            
            # Build
            builder = DicomBuilder.start_patient(pat_id, pat_name)
            study = builder.add_study(study_uid, datetime.date.today())
            series = study.add_series(series_uid, "CT", 1)
            series.set_equipment(man, model, f"SN-{random.randint(1000,9999)}")
            
            # Create Instance
            sop_uid = f"{series_uid}.1"
            ib = series.add_instance(sop_uid, "1.2.840.10008.5.1.4.1.1.2", 1)
            ib.set_pixel_data(pixels)
            
            # Essential CT tags
            ib.set_attribute("0028,0010", rows)
            ib.set_attribute("0028,0011", cols)
            ib.set_attribute("0028,0100", 16) # BitsAllocated
            ib.set_attribute("0028,0101", 12) # BitsStored
            ib.set_attribute("0028,0102", 11) # HighBit
            ib.set_attribute("0028,0103", 0)  # PixelRepresentation (unsigned)
            ib.set_attribute("0028,0004", "MONOCHROME2")
            ib.set_attribute("0008,0060", "CT")
            
            # Additional CT mandatory tags
            ib.set_attribute("0018,0050", "2.0") # SliceThickness
            ib.set_attribute("0018,0060", "120") # KVP
            ib.set_attribute("0020,0032", ["0.0", "0.0", "0.0"]) # ImagePositionPatient
            ib.set_attribute("0020,0037", ["1.0", "0.0", "0.0", "0.0", "1.0", "0.0"]) # ImageOrientationPatient
            ib.set_attribute("0028,0030", ["0.5", "0.5"]) # PixelSpacing
            ib.set_attribute("0028,0008", frames) # NumberOfFrames (Required for Multi-frame)
            
            ib.end_instance()
            series.end_series()
            study.end_study()
            
            p = builder.build()
            
            # Export
            DicomExporter.write_tree(p, output_dir)

    print("Done.")

if __name__ == "__main__":
    main()
