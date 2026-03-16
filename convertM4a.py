import os
import subprocess
from datetime import datetime

def convert_with_logging(input_directory, output_directory):
    extensions = ('.mp4', '.mp3', '.aac', '.amr', '.MP3')
    
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)

    log_path = os.path.join(output_directory, "conversion_log.txt")
    
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n--- Session Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        for filename in os.listdir(input_directory):
            if filename.lower().endswith(extensions):
                input_path = os.path.join(input_directory, filename)
                base_name = os.path.splitext(filename)[0]
                output_path = os.path.join(output_directory, f"{base_name}.m4a")
                
                if os.path.exists(output_path):
                    status = f"SKIPPED: {filename} (Already exists)"
                    print(status)
                    log_file.write(status + "\n")
                    continue
                
                print(f"Converting: {filename}...")
                
                command = [
                    'ffmpeg', '-i', input_path,
                    '-vn', 
                    '-c:a', 'aac', 
                    '-b:a', '192k', # Added quotes here to fix SyntaxError
                    output_path,
                    '-y'
                ]
                
                try:
                    # Added encoding and errors to handle Arabic filenames in the shell output
                    result = subprocess.run(
                        command, 
                        check=True, 
                        capture_output=True, 
                        text=True, 
                        encoding='utf-8', 
                        errors='replace'
                    )
                    status = f"SUCCESS: {filename}"
                    log_file.write(status + "\n")
                except subprocess.CalledProcessError as e:
                    status = f"ERROR: {filename} | Message: {e.stderr}"
                    print(status)
                    log_file.write(status + "\n")

        log_file.write(f"--- Session Ended ---\n")

if __name__ == "__main__":
    source = r"D:\claudes\khutba\audio"
    destination = r"D:\claudes\khutba\audio\converted_m4a"
    
    convert_with_logging(source, destination)
    print(f"\nProcess complete. Log: {os.path.join(destination, 'conversion_log.txt')}")