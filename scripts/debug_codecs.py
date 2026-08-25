
import sys
import os
import pydicom
import pydicom.config
from pydicom.uid import JPEGLosslessSV1

print(f"Python Version: {sys.version}")

# 1. Check Handlers in Isocenter
try:
    import isocenter
    print("\nIsocenter imported successfully.")
except ImportError as e:
    print(f"\nIsocenter import failed: {e}")

print("\n--- Active Pydicom Handlers ---")
for h in pydicom.config.pixel_data_handlers:
    print(f" - {h}")

# 2. Check Imagecodecs
print("\n--- Imagecodecs Check ---")
try:
    import imagecodecs
    print(f"imagecodecs version: {imagecodecs.__version__}")
    print(f"imagecodecs location: {imagecodecs.__file__}")
    
    # List all jpeg related functions
    print("Available JPEG functions:")
    for attr in dir(imagecodecs):
        if 'jpeg' in attr.lower() and 'decode' in attr:
             print(f" - {attr}")
    
except ImportError:
    print("imagecodecs NOT INSTALLED")

# 3. Test Decode (Simulation)
print("\n--- Decoding Simulation (JPEG Lossless) ---")
try:
    # 1.2.840.10008.1.2.4.70
    # This usually requires the 'jpeg' codec in imagecodecs, but specifically capable of lossless.
    # We can't easily synthesize a valid JPEG Lossless bitstream without a library, 
    # but we can check if the function accepts the 'lossless' argument or similar.
    
    if hasattr(imagecodecs, 'jpeg_decode'):
        print("Attempting to call jpeg_decode with garbage data...")
        try:
            imagecodecs.jpeg_decode(b'\xFF\xD8\xFF\xE0\x00\x10JFIF')
        except Exception as e:
            print(f"Result (Expected Failure): {e}")
            # Identify if it's "not a JPEG" vs "symbol lookup error"
            
except Exception as e:
    print(f"Simulation Setup Failed: {e}")

print("\n--- UID Support ---")
from isocenter import imagecodecs_handler
print(f"Handler supports .70: {imagecodecs_handler.supports_transfer_syntax(JPEGLosslessSV1)}")
