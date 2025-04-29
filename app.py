import os
# import sqlite3 # No longer needed directly for DB operations
import requests # To make API calls to the Data Server
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
import dropbox
from datetime import datetime
import random
import string
from werkzeug.utils import secure_filename
import logging

# Logging Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
# অবশ্যই এই সিক্রেট কী প্রোডাকশনের জন্য পরিবর্তন করুন
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'another-very-secure-default-secret-key')

# Configuration
UPLOAD_FOLDER = 'photos' # Local cache folder for this server
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload size

# --- Data Server API Configuration ---
# Data Server এর ঠিকানা (যদি অন্য মেশিনে চলে তবে IP/ডোমেইন পরিবর্তন করুন)
DATA_SERVER_URL = os.environ.get('DATA_SERVER_URL', 'https://itachi321.pythonanywhere.com')
API_TIMEOUT = 10 # সেকেন্ড (API কলের জন্য টাইমআউট)

# Dropbox configuration (পরিবেশ ভেরিয়েবল থেকে লোড করা ভালো)
DROPBOX_APP_KEY = os.environ.get('DROPBOX_APP_KEY', 'b3upyuygczb57te') # আপনার কী দিন
DROPBOX_APP_SECRET = os.environ.get('DROPBOX_APP_SECRET', '2atpq0e01in3yeg') # আপনার সিক্রেট দিন
DROPBOX_REFRESH_TOKEN = os.environ.get('DROPBOX_REFRESH_TOKEN', 'ikqHV2iZDkUAAAAAAAAAAWD2gat2UPsT90MUqor_cGlxy3lkxTYCddvQ7ECZ73Th') # আপনার রিফ্রেশ টোকেন দিন


# --- Helper Functions ---

def get_dropbox_client():
    """Creates and returns a Dropbox client instance."""
    if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
         logging.error("Dropbox credentials missing (APP_KEY, APP_SECRET, REFRESH_TOKEN).")
         return None
    try:
        dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
        )
        dbx.users_get_current_account() # Test connection
        logging.info("Dropbox client created and connection verified.")
        return dbx
    except dropbox.exceptions.AuthError as e:
        logging.error(f"Dropbox authentication error: {e}. Check your REFRESH_TOKEN.")
        return None
    except Exception as e:
        logging.error(f"Error creating Dropbox client: {e}")
        return None

def allowed_file(filename):
    """Checks if the file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def check_id_exists_via_api(image_id):
    """Checks if an ID exists by calling the Data Server API."""
    api_url = f"{DATA_SERVER_URL}/api/check_id/{image_id}"
    try:
        response = requests.get(api_url, timeout=API_TIMEOUT)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        logging.debug(f"API check_id response for '{image_id}': {data}")
        return data.get('exists', False) # Default to False if key missing
    except requests.exceptions.Timeout:
        logging.error(f"API call timeout checking ID: {image_id} at {api_url}")
        raise ConnectionError("Timeout connecting to Data Server")
    except requests.exceptions.RequestException as e:
        logging.error(f"API call error checking ID '{image_id}': {e}")
        raise ConnectionError(f"Could not connect to Data Server: {e}")
    except Exception as e:
        logging.error(f"Unexpected error checking ID '{image_id}' via API: {e}")
        raise ConnectionError("Unexpected error during API call")


def generate_unique_id_via_api():
    """Generates a unique ID, verifying uniqueness via the Data Server API."""
    max_attempts = 10 # Prevent infinite loop
    for attempt in range(max_attempts):
        # Generate random ID (XX-XX-XX-XX format)
        id_parts = [''.join(random.choices(string.ascii_uppercase + string.digits, k=2)) for _ in range(4)]
        image_id = '-'.join(id_parts)
        try:
            if not check_id_exists_via_api(image_id):
                logging.info(f"Generated unique ID via API check: {image_id}")
                return image_id
            else:
                logging.warning(f"Generated ID {image_id} already exists (attempt {attempt+1}). Retrying.")
        except ConnectionError as e:
             logging.error(f"Cannot generate unique ID due to API connection error: {e}")
             raise # Re-raise the connection error to be handled by caller
    logging.error(f"Failed to generate a unique ID after {max_attempts} attempts.")
    raise Exception("Could not generate a unique ID")


def add_image_record_via_api(image_data):
    """Adds an image record by calling the Data Server API."""
    api_url = f"{DATA_SERVER_URL}/api/images"
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(api_url, json=image_data, headers=headers, timeout=API_TIMEOUT)
        logging.debug(f"API add image response status: {response.status_code}, data: {response.text}")
        response.raise_for_status() # Check for HTTP errors
        return response.json() # Return the success response from API
    except requests.exceptions.Timeout:
        logging.error(f"API call timeout adding image record: {image_data.get('id')}")
        raise ConnectionError("Timeout connecting to Data Server")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 409:
            logging.warning(f"API add image conflict (409) for ID {image_data.get('id')}: {e.response.text}")
            raise ValueError(f"Conflict: {e.response.json().get('error', 'Duplicate entry')}") # Raise specific error
        else:
            logging.error(f"API HTTP error adding image record {image_data.get('id')}: Status {e.response.status_code}, Response: {e.response.text}")
            raise ConnectionError(f"Data Server error ({e.response.status_code}): {e.response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"API call error adding image record {image_data.get('id')}: {e}")
        raise ConnectionError(f"Could not connect to Data Server: {e}")
    except Exception as e:
        logging.error(f"Unexpected error adding image record '{image_data.get('id')}' via API: {e}")
        raise ConnectionError("Unexpected error during API call")

def search_images_via_api(search_query):
    """Searches for images by calling the Data Server API."""
    api_url = f"{DATA_SERVER_URL}/api/images"
    params = {'search': search_query}
    try:
        response = requests.get(api_url, params=params, timeout=API_TIMEOUT)
        response.raise_for_status()
        images = response.json()
        logging.debug(f"API search found {len(images)} images for query '{search_query}'")
        return images
    except requests.exceptions.Timeout:
        logging.error(f"API call timeout searching images: query='{search_query}'")
        raise ConnectionError("Timeout connecting to Data Server")
    except requests.exceptions.RequestException as e:
        logging.error(f"API call error searching images (query='{search_query}'): {e}")
        raise ConnectionError(f"Could not connect to Data Server: {e}")
    except Exception as e:
        logging.error(f"Unexpected error searching images '{search_query}' via API: {e}")
        raise ConnectionError("Unexpected error during API call")

def get_image_details_via_api(image_id):
     """Gets full image details by ID from the Data Server API."""
     api_url = f"{DATA_SERVER_URL}/api/images/{image_id}"
     try:
         response = requests.get(api_url, timeout=API_TIMEOUT)
         if response.status_code == 404:
             logging.warning(f"API get details: Image ID '{image_id}' not found (404).")
             return None # Indicate not found
         response.raise_for_status()
         image_data = response.json()
         logging.debug(f"API get details success for ID '{image_id}'.")
         return image_data
     except requests.exceptions.Timeout:
        logging.error(f"API call timeout getting details for ID: {image_id}")
        raise ConnectionError("Timeout connecting to Data Server")
     except requests.exceptions.RequestException as e:
        logging.error(f"API call error getting details for ID '{image_id}': {e}")
        raise ConnectionError(f"Could not connect to Data Server: {e}")
     except Exception as e:
        logging.error(f"Unexpected error getting details for ID '{image_id}' via API: {e}")
        raise ConnectionError("Unexpected error during API call")


def get_dropbox_path_via_api(filename):
    """Gets the Dropbox path for a filename by calling the Data Server API."""
    api_url = f"{DATA_SERVER_URL}/api/image_by_filename/{filename}"
    try:
        response = requests.get(api_url, timeout=API_TIMEOUT)
        if response.status_code == 404:
            logging.warning(f"API get dropbox_path: Filename '{filename}' not found (404).")
            return None # Indicate not found
        response.raise_for_status()
        data = response.json()
        logging.debug(f"API get dropbox_path success for filename '{filename}'.")
        return data.get('dropbox_path') # Return only the path or None
    except requests.exceptions.Timeout:
        logging.error(f"API call timeout getting dropbox_path for filename: {filename}")
        raise ConnectionError("Timeout connecting to Data Server")
    except requests.exceptions.RequestException as e:
        logging.error(f"API call error getting dropbox_path for filename '{filename}': {e}")
        raise ConnectionError(f"Could not connect to Data Server: {e}")
    except Exception as e:
        logging.error(f"Unexpected error getting dropbox_path for '{filename}' via API: {e}")
        raise ConnectionError("Unexpected error during API call")


# --- Flask Routes ---

@app.context_processor
def inject_now():
    """Injects datetime object for use in templates."""
    return {'now': datetime.utcnow}

@app.route('/', methods=['GET', 'POST'])
def index():
    """Handles search requests and displays results."""
    search_query = ''
    images = []
    search_attempted = False

    if request.method == 'POST':
        search_query = request.form.get('search', '').strip()
        search_attempted = True
        if search_query:
            try:
                images = search_images_via_api(search_query)
            except ConnectionError as e:
                flash(f"Error connecting to Data Service: {e}", "error")
            except Exception as e:
                 flash(f"An unexpected error occurred during search: {e}", "error")
        else:
            flash("Please enter a search term.", "error")

    return render_template('index.html', images=images, search_query=search_query, search_attempted=search_attempted)

@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
    """Handles the file upload process."""
    uploaded_image_info = None
    current_custom_id = request.form.get('image_id', '') if request.method == 'POST' else '' # Preserve on error

    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part selected.', 'error')
            return render_template('upload.html', uploaded_image_info=None, current_custom_id=current_custom_id)

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return render_template('upload.html', uploaded_image_info=None, current_custom_id=current_custom_id)

        custom_id = request.form.get('image_id', '').strip()

        if file and allowed_file(file.filename):
            image_id_to_use = None
            try:
                if custom_id:
                    if len(custom_id) > 50 or not all(c.isalnum() or c in '-_' for c in custom_id):
                        flash('Invalid custom ID format. Use letters, numbers, hyphens, underscores (max 50 chars).', 'error')
                        return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)

                    if check_id_exists_via_api(custom_id):
                        flash(f'Error: ID "{custom_id}" already exists.', 'error')
                        return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)
                    image_id_to_use = custom_id
                    logging.info(f"Using validated custom ID: {image_id_to_use}")
                else:
                    image_id_to_use = generate_unique_id_via_api()
                    logging.info(f"Using generated unique ID: {image_id_to_use}")

            except ConnectionError as e:
                flash(f"Error checking ID with Data Service: {e}", "error")
                return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)
            except Exception as e:
                flash(f"Error determining image ID: {e}", "error")
                return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)

            if image_id_to_use:
                original_filename = secure_filename(file.filename)
                filename = f"{image_id_to_use}-{original_filename}"
                local_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                dropbox_path = f"/{filename}"

                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

                try:
                    file.save(local_path)
                    logging.info(f"File saved locally: {local_path}")
                except Exception as e:
                    logging.error(f"Error saving file locally '{local_path}': {e}")
                    flash(f'Error saving file locally: {e}', 'error')
                    # Clean up potential failed save attempt? Unlikely needed.
                    return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)

                dbx = get_dropbox_client()
                if not dbx:
                    flash('Upload failed: Could not connect to cloud storage service.', 'error')
                    if os.path.exists(local_path): os.remove(local_path)
                    return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)

                try:
                    with open(local_path, 'rb') as f:
                        dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode.overwrite)
                    logging.info(f"File uploaded to Dropbox: {dropbox_path}")

                    image_data_to_register = {
                        "id": image_id_to_use,
                        "filename": filename,
                        "original_filename": original_filename,
                        "dropbox_path": dropbox_path
                    }
                    try:
                        api_response = add_image_record_via_api(image_data_to_register)
                        logging.info(f"Successfully registered image via API. Response: {api_response}")

                        image_url = url_for('serve_photo', filename=filename, _external=True)
                        uploaded_image_info = {
                            'id': image_id_to_use,
                            'filename': original_filename,
                            'url': image_url
                        }
                        flash('File uploaded and registered successfully!', 'success')
                        return render_template('upload.html', uploaded_image_info=uploaded_image_info)

                    except ValueError as e: # Specific error for Conflict (409)
                         flash(f"Error registering image: {e}", "error")
                         try: dbx.files_delete_v2(dropbox_path)
                         except Exception as del_e: logging.error(f"Cleanup failed: Could not delete {dropbox_path} after API conflict: {del_e}")
                         if os.path.exists(local_path): os.remove(local_path)
                         return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)
                    except ConnectionError as e:
                         flash(f"Error registering image with Data Service: {e}", "error")
                         try: dbx.files_delete_v2(dropbox_path)
                         except Exception as del_e: logging.error(f"Cleanup failed: Could not delete {dropbox_path} after API connection error: {del_e}")
                         if os.path.exists(local_path): os.remove(local_path)
                         return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)
                    # Catch other potential exceptions during API call
                    except Exception as e:
                         flash(f"Unexpected error during image registration: {e}", "error")
                         try: dbx.files_delete_v2(dropbox_path)
                         except Exception as del_e: logging.error(f"Cleanup failed: Could not delete {dropbox_path} after unexpected registration error: {del_e}")
                         if os.path.exists(local_path): os.remove(local_path)
                         return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)


                except dropbox.exceptions.ApiError as e:
                    logging.error(f"Dropbox API error during upload of {filename}: {e}")
                    flash(f'Cloud storage upload error: {e}', 'error')
                    if os.path.exists(local_path): os.remove(local_path)
                    return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)
                except Exception as e:
                    logging.error(f"Unexpected error during Dropbox upload for {filename}: {e}")
                    flash(f'An unexpected error occurred during upload: {e}', 'error')
                    if os.path.exists(local_path): os.remove(local_path)
                    # Attempt to clean up Dropbox if file might have been uploaded (no guarantee dbx exists here though)
                    if 'dbx' in locals() and dbx:
                         try: dbx.files_delete_v2(dropbox_path)
                         except Exception: pass
                    return render_template('upload.html', uploaded_image_info=None, current_custom_id=custom_id)

        else: # File not allowed type
             allowed_types = ", ".join(ALLOWED_EXTENSIONS)
             flash(f'Invalid file type. Allowed types: {allowed_types}', 'error')
             return render_template('upload.html', uploaded_image_info=None, current_custom_id=current_custom_id)

    # GET request: Render the initial upload form
    return render_template('upload.html', uploaded_image_info=None)


@app.route('/photos/<path:filename>')
def serve_photo(filename):
    """Serves photos from local cache, restoring from Dropbox if necessary."""
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return "Invalid filename", 400

    local_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    logging.debug(f"Request to serve photo: {safe_filename}. Local path: {local_path}")

    if not os.path.exists(local_path):
        logging.warning(f"File '{safe_filename}' not found locally. Attempting restore.")
        dropbox_path = None
        try:
            dropbox_path = get_dropbox_path_via_api(safe_filename)
            if not dropbox_path:
                 logging.error(f"Cannot restore: Filename '{safe_filename}' not found by Data Service.")
                 return "Image record not found by data service", 404

    logging.error(f"Cannot restore: Failed to contact Data Service for dropbox path: {e}")
            return "Data service unavailable", 503
        except Exception as e:
             logging.error(f"Cannot restore: Unexpected error getting dropbox path: {e}")
             return "Error retrieving image details", 500

        if dropbox_path:
            dbx = get_dropbox_client()
            if not dbx:
                logging.error("Cannot restore file: Dropbox client unavailable.")
                return "Cloud storage service unavailable", 503

            try:
                logging.info(f"Downloading '{safe_filename}' from Dropbox path '{dropbox_path}' to '{local_path}'...")
                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                dbx.files_download_to_file(local_path, dropbox_path)
                logging.info(f"Successfully restored '{safe_filename}' from Dropbox.")
            except dropbox.exceptions.ApiError as e:
                logging.error(f"Dropbox API error downloading '{dropbox_path}': {e}")
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.error(f"File not found on Dropbox at path: {dropbox_path}. Data record might be outdated.")
                    return "Image not found in cloud storage", 404
                else:
                    return "Error downloading image from storage", 500
            except Exception as e:
                logging.error(f"Unexpected error during restore download of '{safe_filename}': {e}")
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path) # Clean up partial download
                    except OSError as rm_e:
                        logging.error(f"Error cleaning up partial download '{local_path}': {rm_e}")
                return "Error restoring image", 500
        else:
             return "Image not found", 404

    # --- Serve the file ---
    # Check existence AGAIN after potential download attempt
    if os.path.exists(local_path):
         logging.debug(f"Serving file: {local_path}")
         try:
             return send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename)
         except Exception as e:
             # Handle potential errors during file sending (e.g., file removed between check and send)
             logging.error(f"Error serving file '{local_path}' after existence check: {e}")
             return "Error serving file", 500
    else:
        logging.error(f"File '{safe_filename}' could not be served even after restore attempt.")
        return "Image not found", 404


@app.route('/download/<image_id>')
def download_file(image_id):
    """Handles forced download requests for an image ID."""
    logging.info(f"Download request for ID: {image_id}")
    image_info = None
    try:
        image_info = get_image_details_via_api(image_id)
        if not image_info:
            flash('Image not found for the given ID.', 'error')
            return redirect(url_for('index'))

    except ConnectionError as e:
        flash(f"Error contacting Data Service: {e}", "error")
        return redirect(url_for('index'))
    except Exception as e:
        flash(f"An unexpected error occurred retrieving image details: {e}", "error")
        return redirect(url_for('index'))

    filename = image_info.get('filename')
    dropbox_path = image_info.get('dropbox_path')
    original_filename = image_info.get('original_filename', filename) # Fallback for download prompt

    if not filename or not dropbox_path:
         flash('Error: Incomplete image data received from data service.', 'error')
         return redirect(url_for('index'))

    local_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    logging.debug(f"Preparing download for filename: {filename} (ID: {image_id})")

    # --- Ensure file is available locally ---
    if not os.path.exists(local_path):
        logging.info(f"File '{filename}' not present locally for download. Fetching from Dropbox path '{dropbox_path}'.")
        dbx = get_dropbox_client()
        if not dbx:
            flash('Could not connect to cloud storage to retrieve the file.', 'error')
            return redirect(url_for('index'))
        try:
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            dbx.files_download_to_file(local_path, dropbox_path)
            logging.info(f"Downloaded '{filename}' for user download request.")
        except dropbox.exceptions.ApiError as e:
            logging.error(f"Dropbox API error downloading '{filename}' for user request: {e}")
            flash(f'Error retrieving file from cloud storage: {e}', 'error')
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError as rm_e:
                    logging.error(f"Error cleaning up partial download '{local_path}': {rm_e}")
            return redirect(url_for('index'))
        except Exception as e:
             logging.error(f"Unexpected error fetching '{filename}' for download: {e}")
             flash(f'An unexpected error occurred while fetching the file: {e}', 'error')
             if os.path.exists(local_path):
                 try:
                     os.remove(local_path)
                 except OSError as rm_e:
                     logging.error(f"Error cleaning up partial download '{local_path}': {rm_e}")
             return redirect(url_for('index'))

    # --- Serve the file as an attachment ---
    # Indentation of this block is critical
    if os.path.exists(local_path): # **** CHECK INDENTATION HERE ****
        try:
            logging.info(f"Sending file '{filename}' as attachment for download.")
            return send_from_directory(
                app.config['UPLOAD_FOLDER'],
                filename,
                as_attachment=True,
                # Use original filename for the download prompt
                download_name=original_filename
            )
        except Exception as e:
             logging.error(f"Error sending file '{filename}' as attachment: {e}")
             flash('Error preparing the file for download.', 'error')
             return redirect(url_for('index'))
    else: # **** CHECK INDENTATION HERE ****
        # This else corresponds to the 'if os.path.exists(local_path)' right above
        logging.error(f"File '{filename}' unavailable for download after fetch attempt.")
        flash('Could not retrieve the file for download after checking storage.', 'error')
        return redirect(url_for('index'))


# --- Initialization and Run ---
if __name__ == '__main__':
    # Ensure local upload folder exists for Photo Server
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        try:
            os.makedirs(app.config['UPLOAD_FOLDER'])
            logging.info(f"Created upload folder: {app.config['UPLOAD_FOLDER']}")
        except OSError as e:
            logging.warning(f"Could not create upload folder '{app.config['UPLOAD_FOLDER']}': {e}. File serving/restore might fail.")

    # Optional: Check connectivity to Data Server on startup
    try:
        logging.info(f"Checking connection to Data Server at {DATA_SERVER_URL}...")
        health_url = f"{DATA_SERVER_URL}/api/health"
        response = requests.get(health_url, timeout=5)
        response.raise_for_status()
        if response.json().get("status") == "healthy":
             logging.info("Data Server connection check successful.")
        else:
             logging.warning("Data Server connection check returned unexpected status.")
    except requests.exceptions.RequestException as e:
         logging.error(f"CRITICAL: Could not connect to Data Server at {health_url}. Ensure it is running. Error: {e}")
         # exit(1) # Consider exiting if Data Server is essential

    logging.info("Starting Photo Upload Server...")
    # Run on a different port than the data server
    app.run(host="0.0.0.0", port=5000, debug=True) # Use debug=False in production
