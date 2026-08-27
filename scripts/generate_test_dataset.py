import os
import sys
import random
import uuid
import datetime
import numpy as np
from typing import List, Dict, Any

# Ensure we can import isocenter
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from isocenter.builders import DicomBuilder
from isocenter.io_handlers import DicomExporter
from isocenter.entities import Patient, DicomItem

def generate_random_date(start_year=1950, end_year=2000):
    start_date = datetime.date(start_year, 1, 1)
    end_date = datetime.date(end_year, 12, 31)
    time_between_dates = end_date - start_date
    days_between_dates = time_between_dates.days
    random_number_of_days = random.randrange(days_between_dates)
    return start_date + datetime.timedelta(days=random_number_of_days)

def generate_phi() -> Dict[str, Any]:
    """Generates a dictionary of random PHI."""
    import faker
    fake = faker.Faker()
    
    sex = random.choice(["M", "F", "O"])
    fname = fake.first_name_male() if sex == "M" else fake.first_name_female() if sex == "F" else fake.first_name()
    lname = fake.last_name()
    
    return {
        "PatientName": f"{lname}^{fname}",
        "PatientID": f"PID-{fake.numerify('#####')}",
        "PatientBirthDate": fake.date_of_birth(minimum_age=18, maximum_age=90).strftime("%Y%m%d"),
        "PatientSex": sex,
        "AccessionNumber": f"ACC-{fake.numerify('######')}",
        "StudyID": f"STY-{fake.numerify('####')}",
        "StudyDescription": fake.bs().title(),
        "InstitutionName": fake.company(),
        "ReferringPhysicianName": f"Dr. {fake.name()}",
        "OperatorsName": f"Tech {fake.last_name()}",
    }

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow (PIL) is required. Please install it with `pip install pillow`")
    sys.exit(1)

def create_pixel_data(rows: int, cols: int, frames: int = 1, samples: int = 1, text: str = None):
    """Creates a pattern image with specific dimensions, color depth, and burned-in text."""
    
    # Helper to generate a single frame with PIL
    def generate_single_frame_pil(r, c, s, txt, frame_idx=0):
        # Create base image
        if s == 1:
            mode = "L" # 8-bit grayscale for drawing simplicity
            bg_color = 50
        else:
            mode = "RGB"
            bg_color = (50, 50, 50)
            
        img = Image.new(mode, (c, r), color=bg_color)
        draw = ImageDraw.Draw(img)
        
        # Draw some random shapes
        # Use seed based on dimensions/text so it's deterministic per "patient" but varies
        # Actually random is fine provided we re-seed or just use random
        
        # Draw a rectangle
        rect_coords = (random.randint(0, c//2), random.randint(0, r//2), 
                       random.randint(c//2, c), random.randint(r//2, r))
        if s == 1:
            draw.rectangle(rect_coords, outline=200, width=3)
        else:
            draw.rectangle(rect_coords, outline=(200, 100, 100), width=3)
            
        # Draw a circle
        circle_coords = (random.randint(0, c//2), random.randint(0, r//2), 
                         random.randint(c//2, c), random.randint(r//2, r))
        if s == 1:
            draw.ellipse(circle_coords, outline=180, width=2)
        else:
            draw.ellipse(circle_coords, outline=(100, 200, 100), width=2)

        # Draw Text (Burned In PHI)
        if txt:
            # Try to load a generic font, or default
            try:
                # Pro-tip: FreeType font text size is much better, but might not be available
                # On mac, Arial usually exists.
                font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
                if not os.path.exists(font_path):
                     font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                
                if os.path.exists(font_path):
                    fontsize = int(min(r, c) / 15)
                    font = ImageFont.truetype(font_path, fontsize)
                else:
                    font = ImageFont.load_default()
            except:
                font = ImageFont.load_default()

            text_pos = (20, 20 + (frame_idx * 5)) # Move slightly per frame
            
            if s == 1:
                draw.text(text_pos, txt, fill=255, font=font)
                draw.text((20, r-40), "CONFIDENTIAL", fill=255, font=font)
            else:
                draw.text(text_pos, txt, fill=(255, 255, 255), font=font)
                draw.text((20, r-40), "CONFIDENTIAL", fill=(255, 0, 0), font=font)

        return img

    # Generate Loop
    final_arr = None
    
    # Pre-calculate bit depth scale if needed
    # We draw in 8-bit (L/RGB) for Pillow compatibility, then cast/scale if target is uint16
    dtype = np.uint16 if samples == 1 else np.uint8
    
    frame_list = []
    
    for f in range(frames):
        pil_img = generate_single_frame_pil(rows, cols, samples, text, f)
        arr = np.array(pil_img) # (Rows, Cols) or (Rows, Cols, 3)
        
        if samples == 1:
            # Scale up to use generic uint16 range if needed (Mono2 usually 12-16 bit)
            # Typically 0-65535. Pillow L is 0-255.
            arr = arr.astype(np.uint16) * 256
        
        frame_list.append(arr)
        
    if frames > 1:
        # Stack frames: (Frames, Rows, Cols, [Samples])
        stack = np.stack(frame_list, axis=0)
        return stack
    else:
        # Single frame: (Rows, Cols, [Samples])
        return frame_list[0]


def get_modality_specs(modality: str):
    """
    Returns (rows, cols, frames, samples, bit_depth)
    samples=3 implies RGB, samples=1 implies MONOCHROME2
    """
    specs = {
        "CT": (512, 512, 1, 1),
        "MR": (256, 256, 1, 1),
        "US": (480, 640, 10, 3), # Multi-frame RGB
        "DX": (1024, 1024, 1, 1),
        "CR": (1024, 1024, 1, 1),
        "NM": (128, 128, 1, 1),
        "PT": (128, 128, 1, 1),
        "SC": (512, 512, 1, 3), # RGB
        "XA": (512, 512, 5, 1), # Multi-frame Mono
        "RF": (512, 512, 5, 1), # Multi-frame Mono
        "MG": (1024, 1024, 1, 1),
        "OT": (128, 128, 1, 3),  # RGB
        "SR": (0, 0, 0, 0) # Structure Report (No Pixels)
    }
    return specs.get(modality, (128, 128, 1, 1))

def main():
    output_dir = os.path.abspath("test_data/comprehensive_dicoms")
    os.makedirs(output_dir, exist_ok=True)
    print(f"Generating DICOMs in {output_dir}...")
    
    # Configuration
    num_patients_per_machine = 5

    machines_by_modality = {
        "CT": [("GE", "Revolution"), ("Siemens", "Somatom"), ("Philips", "Brilliance")],
        "MR": [("GE", "Discovery"), ("Siemens", "Magnetom"), ("Philips", "Ingenia")],
        "US": [("GE", "Voluson"), ("Siemens", "Acuson"), ("Philips", "EPIQ")],
        "DX": [("Carestream", "DRX"), ("Agfa", "DX-D"), ("Fuji", "FDR")],
        "CR": [("Carestream", "Classic"), ("Agfa", "CR 30-X"), ("Fuji", "FCR")],
        "NM": [("GE", "NM 830"), ("Siemens", "Symbia")],
        "PT": [("GE", "Discovery MI"), ("Siemens", "Biograph")],
        "SC": [("Generic", "ScreenCapture")],
        "XA": [("GE", "Innova"), ("Siemens", "Artis")],
        "RF": [("GE", "Precision"), ("Siemens", "Luminos")],
        "MG": [("Hologic", "Selenia"), ("GE", "Senographe")],
        "MG": [("Hologic", "Selenia"), ("GE", "Senographe")],
        "OT": [("Isocenter", "Test")],
        "SR": [("Isocenter", "ReportSystem")]
    }
    
    patients = []

    for mod, machines in machines_by_modality.items():
        rows, cols, frames, samples = get_modality_specs(mod)
        
        for man, model in machines:
            for _ in range(num_patients_per_machine):
                phi = generate_phi()
                print(f"Generating {mod} ({man} {model}) for {phi['PatientName']}...")

                # Create a unique study for this modality
                study_uid = f"1.2.840.111111.{random.randint(10000, 99999)}"
                study_date = datetime.date.today()
                
                # Create a series
                series_uid = f"{study_uid}.{random.randint(1, 99)}"
                
                # Create pixel data
                # Included Burned-in Text
                if mod == "SR":
                    pixel_data = None
                else:
                    pixel_data = create_pixel_data(rows, cols, frames, samples, text=phi['PatientName'])

                # Build the patient object graph
                builder = DicomBuilder.start_patient(phi["PatientID"], phi["PatientName"])
                
                study_builder = builder.add_study(study_uid, study_date)
                series_builder = study_builder.add_series(series_uid, mod, 1)
                
                # Set Specific Equipment
                series_builder.set_equipment(man, model, f"SN-{random.randint(1000,9999)}")

                # Create 1 instance per series
                for k in range(1, 2):
                    sop_uid = f"{series_uid}.{k}"
                    sop_class = "1.2.840.10008.5.1.4.1.1.7" # SC fallback
                    # Ideally use correct SOP Class per modality but SC is universally accepted for test
                    
                    inst_builder = series_builder.add_instance(sop_uid, sop_class, k)
                    
                    if mod == "SR":
                        # SR specific logic: No pixels, Add Content Sequence
                        # (0040,A730) Content Sequence
                        
                        # 1. Main Text Content
                        item1 = DicomItem()
                        item1.set_attr("0040,A040", "TEXT") # ValueType
                        item1.set_attr("0040,A160", f"History: Patient {phi['PatientName']} reports pain.") # TextValue (Sensitive!)
                        item1.set_attr("0040,A043", "SEPARATE") # ConceptNameCodeSequence (simplified)
                        
                        # 2. Nested Content
                        nested = DicomItem()
                        nested.set_attr("0040,A040", "PNAME")
                        nested.set_attr("0040,A123", "Dr. Smeagol") # PersonName (Sensitive!)
                        
                        item1.add_sequence_item("0040,A730", nested)
                        
                        # Add to instance directly
                        inst_builder.instance.add_sequence_item("0040,A730", item1)
                        
                        inst_builder.set_attribute("0008,0060", "SR") # Modality
                        inst_builder.set_attribute("0008,0016", "1.2.840.10008.5.1.4.1.1.88.11") # Basic Text SR
                        
                    else:
                        inst_builder.set_pixel_data(pixel_data)
                    
                    # --- Set UIDs Explicitly in Dataset ---
                    inst_builder.set_attribute("0008,0018", sop_uid)
                    inst_builder.set_attribute("0008,0016", sop_class)

                    # --- Set PHI Attributes on Instance ---
                    # Patient Level
                    inst_builder.set_attribute("0010,0030", phi["PatientBirthDate"])
                    inst_builder.set_attribute("0010,0040", phi["PatientSex"])
                    
                    # Study Level
                    inst_builder.set_attribute("0008,0050", phi["AccessionNumber"])
                    inst_builder.set_attribute("0020,0010", phi["StudyID"])
                    inst_builder.set_attribute("0008,1030", f"{phi['StudyDescription']} - {mod}")
                    inst_builder.set_attribute("0008,0090", phi["ReferringPhysicianName"])
                    
                    # Series Level
                    inst_builder.set_attribute("0008,103E", f"{mod} Series Description")
                    inst_builder.set_attribute("0008,0080", phi["InstitutionName"])
                    inst_builder.set_attribute("0008,1070", phi["OperatorsName"])
                    
                    # Instance Level generic tags
                    inst_builder.set_attribute("0020,0032", ["0", "0", "0"]) # Image Position
                    inst_builder.set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"]) # Orientation
                    inst_builder.set_attribute("0028,0030", ["1.0", "1.0"]) # Pixel Spacing
                    inst_builder.set_attribute("0028,0301", "YES") # BurnedInAnnotation
                    
                    inst_builder.end_instance()
                
                series_builder.end_series()
                p = study_builder.end_study().build()
                patients.append(p)

    # Export
    print(f"Exporting {len(patients)} patients...")
    for p in patients:
        DicomExporter.write_tree(p, output_dir)
    
    print("Done!")

if __name__ == "__main__":
    main()
