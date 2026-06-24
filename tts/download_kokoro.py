import os
import urllib.request

def download_file(url, dest):
    if not os.path.exists(dest):
        print(f"Downloading {url} to {dest}...")
        urllib.request.urlretrieve(url, dest)
        print("Done.")
    else:
        print(f"{dest} already exists.")

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Official kokoro-onnx v1.0 model files
    model_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
    voices_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
    
    download_file(model_url, os.path.join(base_dir, "kokoro-v1.0.onnx"))
    download_file(voices_url, os.path.join(base_dir, "voices.bin"))

if __name__ == "__main__":
    main()
