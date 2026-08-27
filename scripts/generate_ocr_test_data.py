import os
import sys
import random
import datetime
import numpy as np
from typing import List, Dict, Any, Tuple

# Ensure we can import isocenter
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from isocenter.builders import DicomBuilder
from isocenter.io_handlers import DicomExporter

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:
    print("Pillow (PIL) is required. Please install it with `pip install pillow`")
    sys.exit(1)

try:
    import faker
except ImportError:
    print("Faker is required. Please install it with `pip install faker`")
    sys.exit(1)

# Initialize Faker
fake = faker.Faker()

def generate_phi() -> Dict[str, Any]:
    """Generates a dictionary of random PHI."""
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
    }

def get_font(size: int):
    """Try to load a system font, otherwise default."""
    # List of common font paths on macOS/Linux
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"
    ]
    
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                continue
    return ImageFont.load_default()

def create_noisy_background(width: int, height: int) -> Image.Image:
    """Creates a noisy background resembling ultrasound texture."""
    # Create random noise
    noise = np.random.randint(0, 100, (height, width), dtype=np.uint8)
    img = Image.fromarray(noise, mode='L')
    return img.filter(ImageFilter.GaussianBlur(radius=1))

def draw_burned_in_text(img: Image.Image, text_lines: List[str], position: str = "top-left", color=255, blur_radius=0, offset: Tuple[int, int] = (0, 0)):
    """Draws text onto the image at the specified position. Supports blur and positional offset."""
    
    # Create a separate layer for text to apply filters independently if needed
    # For now, drawing directly is fine, but if we blur ONLY text, we need a mask/layer.
    # To support Blur properly without blurring background, we make a transparent overlay.
    
    txt_layer = Image.new('RGBA', img.size, (0,0,0,0))
    draw = ImageDraw.Draw(txt_layer)
    w, h = img.size
    
    # Calculate font size relative to image height
    font_size = max(12, int(h * 0.03)) # 3% of height
    font = get_font(font_size)
    
    line_spacing = font_size + 4
    
    # Calculate starting coordinates
    if position == "top-left":
        x, y = 10, 10
    elif position == "top-right":
        x, y = w - 200, 10
        max_width = 0
        for line in text_lines:
             bbox = draw.textbbox((0, 0), line, font=font)
             max_width = max(max_width, bbox[2] - bbox[0])
        x = w - max_width - 10
    elif position == "bottom-left":
        x, y = 10, h - (len(text_lines) * line_spacing) - 10
    elif position == "bottom-right":
         max_width = 0
         for line in text_lines:
             bbox = draw.textbbox((0, 0), line, font=font)
             max_width = max(max_width, bbox[2] - bbox[0])
         x = w - max_width - 10
         y = h - (len(text_lines) * line_spacing) - 10
    else:
        x, y = 10, 10
    
    # Apply Offset
    x += offset[0]
    y += offset[1]

    # Ensure we use a valid RGBA color for the temporary layer
    # If standard 0-255 int passed, map to (C, C, C, 255)
    # If 3-tuple passed, map to (R, G, B, 255)
    # If 4-tuple passed, use as is
    
    rgba_fill = (255, 255, 255, 255) # Default white
    
    if isinstance(color, int):
        rgba_fill = (color, color, color, 255)
    elif isinstance(color, tuple):
        if len(color) == 3:
            rgba_fill = (color[0], color[1], color[2], 255)
        elif len(color) == 4:
            rgba_fill = color
    
    for i, line in enumerate(text_lines):
        draw.text((x, y + i * line_spacing), line, fill=rgba_fill, font=font)
    
    # Apply blur to text layer if requested
    if blur_radius > 0:
        txt_layer = txt_layer.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        
    # Composite back
    if img.mode == 'L':
         # Convert text layer to grayscale for pasting, but keep alpha for mask
         # r, g, b, a = txt_layer.split()
         # weighted conversion is better: L = R * 299/1000 + G * 587/1000 + B * 114/1000
         # But usually simple R or G or B is sufficient if white
         
         # Convert RGBA -> LA (Luminance + Alpha)
         la_layer = txt_layer.convert("LA")
         l, a = la_layer.split()
         img.paste(l, mask=a)
    # Composite back
    if img.mode == 'L':
         # Convert text layer to grayscale for pasting, but keep alpha for mask
         # r, g, b, a = txt_layer.split()
         # weighted conversion is better: L = R * 299/1000 + G * 587/1000 + B * 114/1000
         # But usually simple R or G or B is sufficient if white
         
         # Convert RGBA -> LA (Luminance + Alpha)
         la_layer = txt_layer.convert("LA")
         l, a = la_layer.split()
         img.paste(l, mask=a)
    else:
         img.paste(txt_layer, (0,0), txt_layer)

def create_sc_image_rgb(width=512, height=512, phi_data=None, text_color=(255, 255, 255)):
    """Create a Secondary Capture style RGB image (black background, white text)."""
    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    
    # Draw some "medical content" (a box)
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 100, width-50, height-100], outline=(100, 100, 100), width=2)
    draw.text((width//2 - 50, height//2), "SCAN DATA", fill=(100, 200, 100))
    
    if phi_data:
        lines = [
            f"NAME: {phi_data.get('PatientName', 'DOE^JOHN')}",
            f"ID: {phi_data.get('PatientID', '12345')}",
            f"DOB: {phi_data.get('PatientBirthDate', '19800101')}",
            f"ACC: {phi_data.get('AccessionNumber', 'ACC1')}"
        ]
        draw_burned_in_text(img, lines, position="top-left", color=text_color)
        
        # Add Hospital Name bottom right
        draw_burned_in_text(img, [phi_data.get('InstitutionName', 'General Hospital')], position="bottom-right", color=(200, 200, 200))
        
    return np.array(img)

def create_us_image_mono(width=640, height=480, phi_data=None, text_color=255):
    """Create an Ultrasound style monochrome image (noisy background, sector, text)."""
    img = create_noisy_background(width, height)
    draw = ImageDraw.Draw(img)
    
    # Draw fake sector
    # A white-ish triangle/cone
    draw.pieslice([width//2 - 200, 50, width//2 + 200, 600], 30, 150, fill=150, outline=200)

    if phi_data:
        # Header info
        header_lines = [
            f"{phi_data.get('InstitutionName')}",
            f"{phi_data.get('PatientName').replace('^', ', ')}",
            f"{phi_data.get('PatientID')}",
            f"DOB: {phi_data.get('PatientBirthDate')}"
        ]
        draw_burned_in_text(img, header_lines, position="top-left", color=text_color)
        
        # Date/Time top right
        date_lines = [
            datetime.datetime.now().strftime("%d/%m/%Y"),
            datetime.datetime.now().strftime("%H:%M:%S"),
            "Kbps: 120",
            "MI: 0.7"
        ]
        draw_burned_in_text(img, date_lines, position="top-right", color=text_color)
        
    return np.array(img)

def create_ct_image(width=512, height=512, phi_data=None, blur_radius=0, text_color=255):
    """Create a CT style image (Circle reconstruction, greyscale)."""
    img = Image.new("L", (width, height), color=0)
    draw = ImageDraw.Draw(img)
    
    # Draw "body"
    draw.ellipse([50, 50, width-50, height-50], fill=100)
    
    # Draw "spine" (white blob)
    draw.ellipse([width//2 - 20, height-100, width//2 + 20, height-60], fill=200)
    
    # Draw internal structure
    for _ in range(5):
        x = random.randint(100, width-100)
        y = random.randint(100, height-100)
        draw.ellipse([x, y, x+30, y+30], fill=50)

    if phi_data:
        # Typical CT overlay is corner text
        lines_left = [
            f"Ex: 12345",
            f"Se: 1/1",
            f"Im: 12/90",
            f"Om: {random.randint(0, 500)}",
            f"{phi_data.get('InstitutionName')}"
        ]
        
        lines_right = [
            f"{phi_data.get('PatientName')}",
            f"{phi_data.get('PatientID')}",
            f"DOB: {phi_data.get('PatientBirthDate')}",
            f"Kbps: 120"
        ]
        
        draw_burned_in_text(img, lines_left, position="top-left", color=text_color, blur_radius=blur_radius)
        draw_burned_in_text(img, lines_right, position="top-right", color=text_color, blur_radius=blur_radius)
        
    return np.array(img)

def create_xa_image_sequence(width=512, height=512, frames=10, phi_data=None, jitter=False, text_color=255):
    """Create an XA (Angiography) style monochrome sequence (inverted X-ray look)."""
    # XA is usually bones/vessels (dark) on light background or vice versa. 
    # Let's do dark background, bright vessels (subtracted).
    
    sequence = []
    
    # Static background noise
    bg_noise = np.random.randint(0, 50, (height, width), dtype=np.uint8)
    
    for f in range(frames):
        # Base frame
        img_arr = bg_noise.copy()
        
        # Determine vessel position (moving)
        offset = int((f / frames) * 100)
        
        # Draw vessel (simple line/curve) using PIL
        pil_img = Image.fromarray(img_arr, mode='L')
        draw = ImageDraw.Draw(pil_img)
        
        # Draw "vessel"
        vessel_coords = [
            (100 + offset, 100),
            (150 + offset, 200),
            (150 + offset, 300),
            (200 + offset, 400)
        ]
        draw.line(vessel_coords, fill=200, width=15)
        
        # Burned in text (Static across frames, or Jitter)
        if phi_data:
            lines = [
                f"{phi_data.get('PatientName')}",
                f"{phi_data.get('PatientID')}",
                f"{phi_data.get('InstitutionName')}"
            ]
            
            # Apply Jitter if requested
            jx, jy = 0, 0
            if jitter:
                jx = random.randint(-5, 5)
                jy = random.randint(-5, 5)
            
            draw_burned_in_text(pil_img, lines, position="top-right", color=text_color, offset=(jx, jy))
            
            # Frame counter
            draw.text((10, height-20), f"Fn: {f}", fill=text_color)

        sequence.append(np.array(pil_img))

    # Stack: (Frames, Rows, Cols)
    return np.stack(sequence, axis=0)

def main():
    output_base_dir = os.path.abspath("test_data/ocr_test_set")
    if os.path.exists(output_base_dir):
        import shutil
        shutil.rmtree(output_base_dir)
    os.makedirs(output_base_dir, exist_ok=True)
    
    print(f"Generating OCR Test Set in {output_base_dir}...")
    
    # Scenario 1: Clean SC (No PHI pixels, but PHI in metadata) - Control
    # Scenario 2: Burned-in SC (PHI in pixels AND metadata)
    # Scenario 3: Burned-in US (Noisy, Single Frame)
    # Scenario 4: Burned-in XA (Multi-frame + Jitter)
    # Scenario 5: Burned-in US (Multi-frame)
    # Scenario 6: Burned-in CT (Standard)
    # Scenario 7: Burned-in CT (Blurred)
    
    scenarios = [
        {"type": "SC", "burned_in": False, "count": 1, "frames": 1},
        {"type": "SC", "burned_in": True, "count": 1, "frames": 1},
        {"type": "US", "burned_in": True, "count": 1, "frames": 1},
        {"type": "XA", "burned_in": True, "count": 1, "frames": 15, "jitter": True},
        {"type": "US", "burned_in": True, "count": 1, "frames": 10, "subtype": "CINE"},
        {"type": "CT", "burned_in": True, "count": 2, "frames": 1},
        {"type": "CT", "burned_in": True, "count": 2, "frames": 1, "blur": 1.5}
    ]
    
    for scn in scenarios:
        modality = scn["type"]
        is_burned = scn["burned_in"]
        count = scn["count"]
        frames = scn["frames"]
        subtype = scn.get("subtype", "")
        jitter = scn.get("jitter", False)
        blur = scn.get("blur", 0)
        
        print(f"Generating {count} {modality}{'('+subtype+')' if subtype else ''} - Burned={is_burned} Fr={frames} Jitter={jitter} Blur={blur}...")
        
        
        for i in range(count):
            phi = generate_phi()
            
            # Varied Text Color (150-255)
            grey_val = random.randint(150, 255)
            if modality == "SC":
                 # For RGB, maybe slight color tint?
                 text_color = (grey_val, grey_val, grey_val)
            else:
                 text_color = grey_val
            
            # Setup Builder
            builder = DicomBuilder.start_patient(phi["PatientID"], phi["PatientName"])
            
            study_uid = f"1.2.840.111111.{random.randint(10000, 99999)}"
            study_date = datetime.date.today()
            study_builder = builder.add_study(study_uid, study_date)
            
            series_uid = f"{study_uid}.{random.randint(1, 99)}"
            series_builder = study_builder.add_series(series_uid, modality, 1)
            series_builder.set_equipment("IsocenterCorp", "OCR-Scanner-2000", "SN-9999")
            
            # Generate Pixels
            if modality == "SC":
                rows, cols = 512, 512
                pixel_data = create_sc_image_rgb(rows, cols, phi_data=phi if is_burned else None, text_color=text_color)
                sop_class = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
            elif modality == "US":
                rows, cols = 480, 640
                if frames > 1:
                    # Quick hack to make multi-frame US
                    # Just replicate single frame for now but add frame counter
                    base = create_us_image_mono(rows, cols, phi_data=phi if is_burned else None, text_color=text_color)
                    seq = []
                    for f in range(frames):
                        img = Image.fromarray(base)
                        d = ImageDraw.Draw(img)
                        d.text((rows//2, 20), f"Frame: {f}", fill=text_color)
                        seq.append(np.array(img))
                    pixel_data = np.stack(seq, axis=0)
                    sop_class = "1.2.840.10008.5.1.4.1.1.3.1" # Ultrasound Multi-frame Image Storage
                else:
                    pixel_data = create_us_image_mono(rows, cols, phi_data=phi if is_burned else None, text_color=text_color)
                    sop_class = "1.2.840.10008.5.1.4.1.1.6.1" # Ultrasound Image Storage
            elif modality == "XA":
                rows, cols = 512, 512
                pixel_data = create_xa_image_sequence(rows, cols, frames=frames, phi_data=phi if is_burned else None, jitter=jitter, text_color=text_color)
                sop_class = "1.2.840.10008.5.1.4.1.1.12.1" # X-Ray Angiographic Image Storage
            elif modality == "CT":
                rows, cols = 512, 512
                pixel_data = create_ct_image(rows, cols, phi_data=phi if is_burned else None, blur_radius=blur, text_color=int(text_color)) # Ensure int
                sop_class = "1.2.840.10008.5.1.4.1.1.2" # CT Image Storage
            else:
                continue

            # Create Instance
            sop_uid = f"{series_uid}.{i+1}"
            inst_builder = series_builder.add_instance(sop_uid, sop_class, i+1)
            
            inst_builder.set_pixel_data(pixel_data)
            inst_builder.set_attribute("0008,0018", sop_uid)
            inst_builder.set_attribute("0008,0016", sop_class)
            
            # Set PHI tags
            inst_builder.set_attribute("0010,0030", phi["PatientBirthDate"])
            inst_builder.set_attribute("0010,0040", phi["PatientSex"])
            inst_builder.set_attribute("0008,0050", phi["AccessionNumber"])
            inst_builder.set_attribute("0020,0010", phi["StudyID"])
            inst_builder.set_attribute("0008,0080", phi["InstitutionName"])
            inst_builder.set_attribute("0028,0301", "YES" if is_burned else "NO") # Burned In Annotation
            
            if modality == "SC":
                 inst_builder.set_attribute("0028,0002", 3) # SamplesPerPixel
                 inst_builder.set_attribute("0028,0004", "RGB")
            else:
                 inst_builder.set_attribute("0028,0002", 1)
                 inst_builder.set_attribute("0028,0004", "MONOCHROME2")
                 if modality == "CT":
                     inst_builder.set_attribute("0028,1050", 40) # WindowCenter
                     inst_builder.set_attribute("0028,1051", 400) # WindowWidth
            
            inst_builder.set_attribute("0028,0010", rows)
            inst_builder.set_attribute("0028,0011", cols)
            
            # Generic Instance Attributes (Position/Spacing)
            inst_builder.set_attribute("0020,0032", ["0", "0", "0"]) # Image Position
            inst_builder.set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"]) # Orientation
            inst_builder.set_attribute("0028,0030", ["1.0", "1.0"]) # Pixel Spacing
            
            if modality == "CT":
                 inst_builder.set_attribute("0018,0050", "2.5") # Slice Thickness
                 inst_builder.set_attribute("0018,0060", "120") # KVP
            
            if frames > 1:
                inst_builder.set_attribute("0028,0008", str(frames))

            inst_builder.end_instance()
            series_builder.end_series()
            patient_obj = study_builder.end_study().build()
            
            # Save
            DicomExporter.write_tree(patient_obj, output_base_dir)

    print("Generation Logic Complete.")

if __name__ == "__main__":
    main()
