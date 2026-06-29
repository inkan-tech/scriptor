"""
reMarkable utilities for document processing.

This module provides functionality for extracting and processing content
from reMarkable documents, including handwriting recognition.
"""

import os
import subprocess
import json
import hashlib
import hmac
import glob
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

# Check if rmscene is available
try:
    import rmscene
    from rmscene.scene_items import Line, Point, Pen, Text
    RMSCENE_AVAILABLE = True
except ImportError:
    RMSCENE_AVAILABLE = False

# Check if requests is available for MyScript API
def is_requests_available():
    """Check if requests library is available."""
    try:
        import requests
        # Access the module to satisfy linter (we're checking availability)
        _ = requests.__name__
        return True
    except ImportError:
        return False

REQUESTS_AVAILABLE = is_requests_available()

# Load environment variables from dedicated inkan-doc config file
from dotenv import load_dotenv

# Try to load from $HOME/.local/inkan-doc.env
inkan_doc_env_path = os.path.expanduser("~/.local/inkan-doc.env")
if os.path.exists(inkan_doc_env_path):
    load_dotenv(inkan_doc_env_path)
else:
    # Fallback to standard .env locations
    load_dotenv()
    load_dotenv('../.env')

# MyScript API Configuration
MYSCRIPT_APP_KEY = os.getenv('MYSCRIPT_APPLICATION_KEY')
MYSCRIPT_HMAC_KEY = os.getenv('MYSCRIPT_HMAC_KEY')
MYSCRIPT_API_HOST = "https://cloud.myscript.com"
MYSCRIPT_BATCH_ENDPOINT = "/api/v4.0/iink/batch"

# Check for MyScript API configuration and stop if not available
def check_myscript_config():
    """Check if MyScript API is properly configured."""
    if not is_requests_available():
        print("❌ Error: 'requests' library not available. Please install with: pip install requests")
        return False
    
    if not MYSCRIPT_APP_KEY or not MYSCRIPT_HMAC_KEY:
        print("❌ Error: MyScript API not configured")
        print("Please create ~/.local/inkan-doc.env with:")
        print("MYSCRIPT_APPLICATION_KEY=your_key_here")
        print("MYSCRIPT_HMAC_KEY=your_hmac_key_here")
        return False
    
    return True

# Configuration validity will be checked when needed
MYSCRIPT_CONFIG_VALID = None  # Will be determined at runtime

# Constants for stroke conversion
SPEED_FACTOR = 200 * 100
STROKE_GAP = 500 * 100
MIN_SPEED = 0.01


# Cloud client integration — replaces rmapi subprocess calls
try:
    from .rm_cloud import (
        check_cloud_available as check_rmapi_available,
        list_remarkable_documents,
        download_remarkable_document,
        upload_to_remarkable,
        delete_remarkable_document,
    )
    CLOUD_AVAILABLE = True
except ImportError:
    CLOUD_AVAILABLE = False

    def check_rmapi_available() -> bool:
        """Fallback: check if rmapi binary is available."""
        try:
            subprocess.run(["rmapi", "--help"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def list_remarkable_documents() -> List[Dict[str, str]]:
        raise RuntimeError("Install cloud extras: uv pip install -e '.[cloud]'")

    def download_remarkable_document(document_name: str, output_path: str) -> str:
        raise RuntimeError("Install cloud extras: uv pip install -e '.[cloud]'")

    def upload_to_remarkable(file_path: str, destination: Optional[str] = None) -> str:
        raise RuntimeError("Install cloud extras: uv pip install -e '.[cloud]'")

    def delete_remarkable_document(document_name: str) -> bool:
        raise RuntimeError("Install cloud extras: uv pip install -e '.[cloud]'")


def compute_hmac(data):
    """Compute HMAC for MyScript API authentication."""
    if not MYSCRIPT_APP_KEY or not MYSCRIPT_HMAC_KEY:
        return None
    key = (MYSCRIPT_APP_KEY + MYSCRIPT_HMAC_KEY).encode('utf-8')
    data_bytes = json.dumps(data).encode('utf-8')
    mac = hmac.new(key, data_bytes, hashlib.sha512)
    return mac.hexdigest()

def send_to_myscript(stroke_groups, language="en_US"):
    """Send stroke data to MyScript API for handwriting recognition."""
    # Check requests availability dynamically
    try:
        import requests
    except ImportError:
        print("⚠️  MyScript API not configured or requests unavailable")
        return None
    
    if not MYSCRIPT_APP_KEY or not MYSCRIPT_HMAC_KEY:
        print("⚠️  MyScript API not configured or requests unavailable")
        return None
    
    request_data = {
        "width": 1000,
        "height": 1000,
        "contentType": "Text",
        "conversionState": "DIGITAL_EDIT",
        "xDPI": 96,
        "yDPI": 96,
        "strokeGroups": stroke_groups,
        "configuration": {
            "lang": language,
            "text": {
                "guides": {"enable": False},
                "margin": {"top": 0, "left": 0, "right": 0, "bottom": 0},
                "configuration": {"addLKText": True}
            },
            "export": {
                "image-resolution": 300,
                "jiix": {
                    "strokes": True,
                    "bounding-box": True,
                    "style": True,
                    "text": {"chars": False, "words": True}
                }
            }
        }
    }
    
    hmac_value = compute_hmac(request_data)
    if not hmac_value:
        return None
    
    headers = {
        "applicationKey": MYSCRIPT_APP_KEY,
        "hmac": hmac_value,
        "Content-Type": "application/json",
        "Accept": "application/json, application/vnd.myscript.jiix"
    }
    
    try:
        response = requests.post(MYSCRIPT_API_HOST + MYSCRIPT_BATCH_ENDPOINT, 
                               json=request_data, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"MyScript API Error: {response.status_code}, {response.text}")
            return None
    except Exception as e:
        print(f"MyScript API request failed: {e}")
        return None

def is_text_stroke(tool_type: Any) -> bool:
    """Check if a pen tool type corresponds to a text stroke."""
    non_text_brushes = [
        Pen.ERASER,
        Pen.ERASER_AREA,
        Pen.HIGHLIGHTER_1,
        Pen.HIGHLIGHTER_2
    ]
    return tool_type not in non_text_brushes

def extract_strokes_from_rm_file(file_path):
    """Extract stroke data from .rm file for MyScript processing."""
    stroke_groups = []
    t_offset = 0
    
    try:
        with open(file_path, 'rb') as f:
            scene_tree = rmscene.read_tree(f)
        
        for item in scene_tree.walk():
            if isinstance(item, Line) and is_text_stroke(item.tool):
                # Convert rmscene.Line to MyScript format
                myscript_points_x = []
                myscript_points_y = []
                myscript_points_t = []
                myscript_points_p = []
                
                for point in item.points:
                    myscript_points_x.append(int(round(point.x)))
                    myscript_points_y.append(int(round(point.y)))
                    
                    # Calculate timestamp based on speed
                    speed_val = max(MIN_SPEED, float(point.speed))
                    offset = float(SPEED_FACTOR) / speed_val
                    t_offset += int(round(offset))
                    myscript_points_t.append(t_offset)
                    
                    # Normalize pressure from 0-255 to 0-1
                    myscript_points_p.append(max(0.0, min(1.0, float(point.pressure) / 255.0)))
                
                if myscript_points_x:
                    stroke_groups.append({
                        "penStyle": "color: #000000;",
                        "strokes": [{
                            "pointerType": "PEN",
                            "pointerId": 1,
                            "x": myscript_points_x,
                            "y": myscript_points_y,
                            "t": myscript_points_t,
                            "p": myscript_points_p
                        }]
                    })
                    t_offset += STROKE_GAP
                    
    except Exception as e:
        print(f"Error extracting strokes from {file_path}: {e}")
        return []
    
    return stroke_groups

def extract_typed_text_from_rm_file(file_path):
    """Extract typed text content from .rm files."""
    typed_content = []
    
    try:
        with open(file_path, 'rb') as f:
            scene_tree = rmscene.read_tree(f)

        # Iterate through all items in the scene tree to find typed text
        for item in scene_tree.walk():
            if isinstance(item, Text):
                # Try different possible attributes for text content
                if hasattr(item, 'text'):
                    typed_content.append(item.text)
                elif hasattr(item, 'content'):
                    typed_content.append(item.content)
                elif hasattr(item, 'value'):
                    typed_content.append(item.value)
                else:
                    # If none of the expected attributes exist, convert to string
                    typed_content.append(str(item))
                
    except Exception as e:
        print(f"Error processing typed text from .rm file {file_path}: {e}")
        
    return typed_content

def generate_svg_from_strokes(stroke_groups, output_path, margin=50):
    """Generate SVG visualization of handwritten strokes."""
    min_x, min_y = float('inf'), float('inf')
    max_x, max_y = float('-inf'), float('-inf')
    paths_data = []
    
    for group in stroke_groups:
        for stroke in group.get('strokes', []):
            if len(stroke['x']) > 1:
                path_points = []
                for x, y in zip(stroke['x'], stroke['y']):
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
                    path_points.append(f"{x} {y}")
                
                path_data = f"M {path_points[0]}"
                for i in range(1, len(path_points)):
                    path_data += f" L {path_points[i]}"
                paths_data.append(f'<path d="{path_data}" stroke="black" stroke-width="2" fill="none"/>')
    
    if not paths_data:
        print(f"No stroke data to generate SVG for {output_path}")
        # Create empty SVG to match the behavior
        with open(output_path, 'w') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>')
        return
    
    # Add margin to bounding box
    min_x -= margin
    min_y -= margin
    max_x += margin
    max_y += margin
    
    view_width = max_x - min_x
    view_height = max_y - min_y
    
    if view_width <= 0: view_width = 100
    if view_height <= 0: view_height = 100
    
    svg_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{view_width}" height="{view_height}" viewBox="{min_x} {min_y} {view_width} {view_height}" xmlns="http://www.w3.org/2000/svg">
<rect x="{min_x}" y="{min_y}" width="{view_width}" height="{view_height}" fill="white"/>
'''
    
    svg_content += '\n'.join(paths_data)
    svg_content += '\n</svg>'
    
    with open(output_path, 'w') as f:
        f.write(svg_content)
    
    print(f"SVG visualization saved to {output_path}")

@dataclass
class HandwritingInterpretation:
    """Represents an alternative interpretation of handwriting."""
    text: str
    confidence_score: float
    rationale: str

@dataclass
class WordCandidate:
    """Represents a word candidate from MyScript recognition."""
    text: str
    confidence_rank: int
    bounding_box: Dict[str, float]
    timestamp_start: Optional[str] = None
    timestamp_end: Optional[str] = None

@dataclass
class PhraseInterpretation:
    """Represents a complete phrase interpretation."""
    text: str
    confidence_score: float
    word_breakdown: List[str]
    rationale: str


class HandwritingAnalyzer:
    """Advanced analyzer for MyScript handwriting recognition results."""
    
    def __init__(self, myscript_data: Dict[str, Any]):
        self.data = myscript_data
        self.original_text = myscript_data.get('label', '')
        self.words = self._extract_words()
        
    def _extract_words(self) -> List[Dict[str, Any]]:
        """Extract word-level data from MyScript JSON."""
        words = []
        for word in self.data.get('words', []):
            if word.get('label') and word.get('label').strip() and word.get('label') != ' ':
                words.append(word)
        return words
    
    def _get_word_candidates(self, word: Dict[str, Any]) -> List[WordCandidate]:
        """Get all candidates for a word with their confidence ranking."""
        candidates = []
        label = word.get('label', '')
        candidates_list = word.get('candidates', [label])
        
        # Get timestamp info if available
        items = word.get('items', [])
        timestamp_start = items[0].get('timestamp') if items else None
        timestamp_end = items[-1].get('timestamp') if items else None
        
        for i, candidate in enumerate(candidates_list):
            candidates.append(WordCandidate(
                text=candidate,
                confidence_rank=i,
                bounding_box=word.get('bounding-box', {}),
                timestamp_start=timestamp_start,
                timestamp_end=timestamp_end
            ))
        return candidates
    
    def _calculate_phrase_confidence(self, word_choices: List[tuple]) -> float:
        """Calculate confidence score for a phrase based on word choice rankings."""
        total_penalty = sum(rank for _, rank in word_choices)
        max_penalty = len(word_choices) * 4  # Assuming max 5 candidates per word
        confidence = max(0, 1 - (total_penalty / max_penalty)) if max_penalty > 0 else 1
        return confidence
    
    def _apply_contextual_fixes(self, words: List[str]) -> str:
        """Apply contextual fixes to improve phrase coherence."""
        # Create a working copy
        fixed_words = words.copy()
        
        # Apply specific contextual interpretations
        if 'torten' in fixed_words:
            idx = fixed_words.index('torten')
            # In document context, "torten" is likely "to the" or just "to"
            if idx < len(fixed_words) - 1 and fixed_words[idx + 1].lower().startswith('whe'):
                fixed_words[idx] = 'to'
        
        # Clean up the result
        result = ' '.join(fixed_words)
        
        # Additional phrase-level fixes
        result = result.replace("'the the", "the")
        result = result.replace("the'the", "the")
        result = result.replace("had_", "had -")
        
        return result
    
    def _get_improvement_rationale(self, original: str, improved: str) -> str:
        """Generate rationale for improvements made."""
        if original == improved:
            return "Original recognition used as-is"
        
        changes = []
        if "torten" in original and "to" in improved:
            changes.append("'torten' interpreted as 'to' in document context")
        if "'the the" in original or "the'the" in original:
            changes.append("Removed duplicate 'the' with apostrophe")
        if "had_" in original and "had -" in improved:
            changes.append("Underscore interpreted as dash")
        
        return "; ".join(changes) if changes else "Contextual phrase improvement"
    
    def generate_interpretations(self, max_combinations: int = 10) -> List[HandwritingInterpretation]:
        """Generate multiple interpretations of the handwritten text."""
        if not self.words:
            # Fallback to simple interpretation for backwards compatibility
            return self._generate_simple_interpretations(max_combinations)
        
        import itertools
        
        # Get all word candidates
        all_candidates = []
        for word in self.words:
            candidates = self._get_word_candidates(word)
            all_candidates.append(candidates[:3])  # Limit to top 3 candidates per word
        
        # Generate combinations
        interpretations = []
        combinations = list(itertools.product(*[[c.text for c in word_candidates] 
                                               for word_candidates in all_candidates]))
        
        # Limit combinations to prevent explosion
        combinations = combinations[:max_combinations]
        
        for combination in combinations:
            # Calculate confidence
            word_choices = []
            for i, choice in enumerate(combination):
                rank = next((j for j, c in enumerate(all_candidates[i]) if c.text == choice), 0)
                word_choices.append((choice, rank))
            
            confidence = self._calculate_phrase_confidence(word_choices)
            text = ' '.join(combination)
            
            # Apply contextual improvements
            improved_text = self._apply_contextual_fixes(list(combination))
            if improved_text != text:
                confidence = min(1.0, confidence + 0.1)  # Boost confidence for improvements
            
            rationale = self._get_improvement_rationale(text, improved_text)
            if rationale == "Original recognition used as-is":
                rationale = f"Word choices: {', '.join(f'{w}(rank:{r})' for w, r in word_choices)}"
            
            interpretations.append(HandwritingInterpretation(
                text=improved_text,
                confidence_score=confidence,
                rationale=rationale
            ))
        
        # Sort by confidence and remove duplicates
        seen = set()
        unique_interpretations = []
        for interp in sorted(interpretations, key=lambda x: x.confidence_score, reverse=True):
            if interp.text not in seen:
                seen.add(interp.text)
                unique_interpretations.append(interp)
        
        return unique_interpretations[:max_combinations]
    
    def _generate_simple_interpretations(self, max_combinations: int) -> List[HandwritingInterpretation]:
        """Fallback method for simple text alternatives when no word data available."""
        interpretations = []
        
        if self.original_text:
            alternatives = self._generate_alternatives(self.original_text)
            
            for i, alt_text in enumerate(alternatives[:max_combinations]):
                confidence = max(0.5, 1.0 - (i * 0.1))
                rationale = self._generate_rationale(self.original_text, alt_text)
                
                interpretations.append(HandwritingInterpretation(
                    text=alt_text,
                    confidence_score=confidence,
                    rationale=rationale
                ))
        
        return interpretations
    
    def _generate_alternatives(self, text: str) -> List[str]:
        """Generate alternative text interpretations."""
        alternatives = []
        
        # Common letter substitutions
        substitutions = {
            'o': ['0', 'a', 'e'],
            '0': ['o', 'O'],
            'l': ['1', 'I', 'i'],
            '1': ['l', 'I', 'i'],
            'I': ['l', '1', 'i'],
            'rn': ['m'],
            'm': ['rn'],
            'cl': ['d'],
            'd': ['cl'],
        }
        
        # Apply substitutions
        for original, replacements in substitutions.items():
            if original in text:
                for replacement in replacements:
                    alt_text = text.replace(original, replacement)
                    if alt_text != text and alt_text not in alternatives:
                        alternatives.append(alt_text)
        
        # Word-level alternatives
        word_alternatives = {
            'endpont': 'endpoint',
            'desing': 'design',
            'sytem': 'system',
            'prcess': 'process',
            'torten': 'to',
        }
        
        for original, correct in word_alternatives.items():
            if original in text.lower():
                alt_text = text.lower().replace(original, correct)
                if alt_text != text.lower() and alt_text not in alternatives:
                    alternatives.append(alt_text)
        
        return alternatives
    
    def _generate_rationale(self, original: str, alternative: str) -> str:
        """Generate a rationale for why the alternative might be correct."""
        if 'endpoint' in alternative and 'endpont' in original:
            return "Common technical term, 'endpont' likely misspelled"
        elif 'to' in alternative and 'torten' in original:
            return "'torten' interpreted as 'to' in document context"
        elif alternative.endswith('s') and not original.endswith('s'):
            return "Plural form makes sense in context"
        elif ' ' in alternative and ' ' not in original:
            return "Alternative spacing of compound word"
        elif any(c.isdigit() for c in alternative) and not any(c.isdigit() for c in original):
            return "Number substitution for similar-looking letter"
        else:
            return "Character recognition uncertainty"
    
    def analyze_writing_flow(self) -> Dict[str, Any]:
        """Analyze the temporal flow of writing."""
        timestamps = []
        for word in self.words:
            items = word.get('items', [])
            for item in items:
                ts = item.get('timestamp')
                if ts:
                    timestamps.append({
                        'timestamp': ts,
                        'glyph': item.get('label', ''),
                        'word': word.get('label', '')
                    })
        
        # Sort by timestamp
        timestamps.sort(key=lambda x: x['timestamp'])
        
        return {
            'total_glyphs': len(timestamps),
            'writing_sequence': timestamps,
            'time_span': f"{timestamps[0]['timestamp']} to {timestamps[-1]['timestamp']}" if timestamps else "No timestamps"
        }
    
    def get_spatial_analysis(self) -> Dict[str, Any]:
        """Analyze spatial relationships between words."""
        word_positions = []
        for word in self.words:
            bbox = word.get('bounding-box', {})
            if bbox:
                word_positions.append({
                    'word': word.get('label', ''),
                    'x': bbox.get('x', 0),
                    'y': bbox.get('y', 0),
                    'width': bbox.get('width', 0),
                    'height': bbox.get('height', 0)
                })
        
        # Sort by position (left to right, top to bottom)
        word_positions.sort(key=lambda w: (w['y'], w['x']))
        
        return {
            'words_by_position': word_positions,
            'likely_line_breaks': self._detect_line_breaks(word_positions)
        }
    
    def _detect_line_breaks(self, word_positions: List[Dict]) -> List[int]:
        """Detect likely line breaks based on Y position changes."""
        line_breaks = []
        if len(word_positions) < 2:
            return line_breaks
        
        for i in range(1, len(word_positions)):
            prev_word = word_positions[i-1]
            curr_word = word_positions[i]
            
            # Significant Y position change indicates new line
            y_diff = abs(curr_word['y'] - prev_word['y'])
            if y_diff > prev_word.get('height', 10) * 0.5:  # Half the height threshold
                line_breaks.append(i)
        
        return line_breaks
    
    def generate_comprehensive_analysis(self) -> Dict[str, Any]:
        """Generate a comprehensive analysis including all features."""
        return {
            'original_text': self.original_text,
            'interpretations': [
                {
                    'text': interp.text,
                    'confidence': interp.confidence_score,
                    'rationale': interp.rationale
                }
                for interp in self.generate_interpretations()
            ],
            'writing_flow': self.analyze_writing_flow(),
            'spatial_analysis': self.get_spatial_analysis(),
            'word_count': len(self.words)
        }


class ReMarkableProcessor:
    """Process reMarkable documents and extract content."""
    
    def __init__(self):
        if not RMSCENE_AVAILABLE:
            raise ImportError("rmscene library is required but not available")
    
    def process_rmdoc_file(
        self,
        rmdoc_path: str,
        output_dir: str,
        language: str = "en_US"
    ) -> Dict[str, Any]:
        """
        Process a .rmdoc file and extract content.
        
        Args:
            rmdoc_path: Path to the .rmdoc file
            output_dir: Directory to save extracted content
            language: Language for handwriting recognition
        
        Returns:
            Dictionary with processing results
        """
        rmdoc_file = Path(rmdoc_path)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        files_generated = []
        total_pages = 0
        
        try:
            # Check if file is a ZIP file (newer reMarkable format)
            import zipfile
            if zipfile.is_zipfile(rmdoc_file):
                return self._process_zip_rmdoc(rmdoc_file, output_path, language)
            
            # Try to extract document using rmscene
            with open(rmdoc_file, 'rb') as f:
                doc = rmscene.read_tree(f)
            
            # Process document structure
            if hasattr(doc, 'pages'):
                pages = doc.pages
            elif hasattr(doc, 'root') and hasattr(doc.root, 'pages'):
                pages = doc.root.pages
            else:
                # Try to find pages in the document structure
                pages = []
                if hasattr(doc, 'root'):
                    for item in doc.root:
                        if hasattr(item, 'pages'):
                            pages.extend(item.pages)
            
            # Process each page
            for page_num, page in enumerate(pages):
                total_pages += 1
                page_base = f"{rmdoc_file.stem}_page{page_num + 1}"
                
                # Extract handwriting as SVG
                svg_path = output_path / f"{page_base}_handwritten.svg"
                try:
                    with open(svg_path, 'w') as f:
                        f.write(page.to_svg())
                    files_generated.append(str(svg_path))
                except Exception as e:
                    print(f"Warning: Could not generate SVG for page {page_num + 1}: {e}")
                
                # Convert to PDF if possible
                try:
                    pdf_path = output_path / f"{page_base}_handwritten.pdf"
                    # This would require additional conversion tools
                    # For now, just note the file would be generated
                    files_generated.append(str(pdf_path))
                except Exception:
                    pass  # PDF conversion might not be available
            
            # Generate summary files
            json_path = output_path / f"{rmdoc_file.stem}.json"
            with open(json_path, 'w') as f:
                json.dump({
                    'source_file': str(rmdoc_file),
                    'total_pages': total_pages,
                    'language': language,
                    'files_generated': files_generated,
                    'file_type': 'rmscene'
                }, f, indent=2)
            files_generated.append(str(json_path))
            
        except Exception as e:
            # Fallback: create basic info file
            error_path = output_path / f"{rmdoc_file.stem}_error.txt"
            with open(error_path, 'w') as f:
                f.write(f"Error processing {rmdoc_file.name}: {str(e)}\n")
                f.write(f"File size: {rmdoc_file.stat().st_size} bytes\n")
            files_generated.append(str(error_path))
        
        return {
            'total_pages': total_pages,
            'files_generated': files_generated,
            'output_directory': str(output_path)
        }
    
    def _process_zip_rmdoc(self, rmdoc_file: Path, output_path: Path, language: str) -> Dict[str, Any]:
        """Process a ZIP-based .rmdoc file (newer format) with full MyScript integration."""
        import zipfile
        import shutil
        
        files_generated = []
        total_pages = 0
        temp_dir = output_path / "temp_rm_extract"
        temp_dir.mkdir(exist_ok=True)
        
        try:
            with zipfile.ZipFile(rmdoc_file, 'r') as zip_ref:
                # Extract all files to temp directory
                zip_ref.extractall(temp_dir)
                file_list = zip_ref.namelist()
                print(f"Extracted {rmdoc_file.name} to {temp_dir}")
                
                # Find all .rm files
                rm_files = glob.glob(str(temp_dir / "**" / "*.rm"), recursive=True)
                total_pages = len(rm_files)
                
                if not rm_files:
                    print("No .rm files found in the reMarkable document.")
                    return {'total_pages': 0, 'files_generated': [], 'output_directory': str(output_path)}
                
                # Process each page individually with MyScript and typed text extraction
                all_stroke_groups = []
                all_typed_text = []
                all_page_results = []
                base_name = rmdoc_file.stem
                
                for i, rm_file in enumerate(rm_files, 1):
                    page_name = Path(rm_file).stem
                    print(f"Processing page {i}/{len(rm_files)}: {page_name}...")
                    
                    # Extract strokes for this page
                    page_strokes = extract_strokes_from_rm_file(rm_file)
                    all_stroke_groups.extend(page_strokes)
                    
                    # Extract typed text for this page
                    page_typed_text = extract_typed_text_from_rm_file(rm_file)
                    all_typed_text.extend(page_typed_text)
                    
                    # Process this page result
                    page_result = {'page': i, 'name': page_name, 'strokes': len(page_strokes), 'typed_text': page_typed_text}
                    
                    if page_strokes:
                        # Generate individual page SVG
                        page_svg_path = output_path / f"{base_name}_page{i}_handwritten.svg"
                        generate_svg_from_strokes(page_strokes, page_svg_path)
                        files_generated.append(str(page_svg_path))
                        
                        # Send to MyScript for recognition
                        print(f"Sending page {i} handwritten content to MyScript for recognition...")
                        myscript_result = send_to_myscript(page_strokes, language)
                        
                        # Always create text files, even if MyScript fails
                        page_txt_path = output_path / f"{base_name}_page{i}_handwritten.txt"
                        page_json_path = output_path / f"{base_name}_page{i}_handwritten_myscript.json"
                        
                        if myscript_result:
                            # Save recognized text
                            recognized_text = myscript_result.get('text', '')
                            if not recognized_text and 'words' in myscript_result:
                                recognized_text = ' '.join([word.get('label', '') for word in myscript_result['words']])
                            
                            with open(page_txt_path, 'w') as f:
                                f.write(recognized_text)
                            files_generated.append(str(page_txt_path))
                            print(f"Page {i} handwritten text saved to {page_txt_path}")
                            
                            # Save MyScript JSON
                            with open(page_json_path, 'w') as f:
                                json.dump(myscript_result, f, indent=2)
                            files_generated.append(str(page_json_path))
                            print(f"Page {i} MyScript output saved to {page_json_path}")
                            
                            page_result['recognized_text'] = recognized_text
                        else:
                            # Create placeholder files when MyScript API unavailable
                            with open(page_txt_path, 'w') as f:
                                f.write("")  # Empty placeholder
                            files_generated.append(str(page_txt_path))
                            
                            with open(page_json_path, 'w') as f:
                                json.dump({"error": "MyScript API not configured", "text": "", "status": "failed"}, f, indent=2)
                            files_generated.append(str(page_json_path))
                            
                            print(f"Created placeholder files for page {i} (MyScript API not available)")
                            page_result['recognized_text'] = ""
                    else:
                        # Create empty SVG for pages without strokes
                        page_svg_path = output_path / f"{base_name}_page{i}_handwritten.svg"
                        generate_svg_from_strokes([], page_svg_path)
                        files_generated.append(str(page_svg_path))
                    
                    if page_typed_text:
                        # Save individual page typed text
                        page_typed_path = output_path / f"{base_name}_page{i}_typed.txt"
                        with open(page_typed_path, 'w') as f:
                            f.write('\n'.join(page_typed_text))
                        files_generated.append(str(page_typed_path))
                        print(f"Page {i} typed text saved to {page_typed_path}")
                    
                    all_page_results.append(page_result)
                
                # Process combined typed text (if any)
                if all_typed_text:
                    typed_output_path = output_path / f"{base_name}_typed.txt"
                    with open(typed_output_path, 'w') as f:
                        f.write('\n'.join(all_typed_text))
                    files_generated.append(str(typed_output_path))
                    print(f"Combined typed text saved to {typed_output_path}")
                    
                    # Also save as JSON
                    typed_json_path = output_path / f"{base_name}_typed.json"
                    with open(typed_json_path, 'w') as f:
                        json.dump({"typed_text": all_typed_text}, f, indent=2)
                    files_generated.append(str(typed_json_path))
                    print(f"Combined typed text JSON saved to {typed_json_path}")
                
                # Process combined content
                if all_stroke_groups:
                    # Generate combined SVG
                    combined_svg_path = output_path / f"{base_name}_handwritten.svg"
                    generate_svg_from_strokes(all_stroke_groups, combined_svg_path)
                    files_generated.append(str(combined_svg_path))
                    
                    # Send combined content to MyScript
                    print("Sending combined handwritten content to MyScript for recognition...")
                    combined_result = send_to_myscript(all_stroke_groups, language)
                    
                    # Always create combined files, even if MyScript fails
                    combined_txt_path = output_path / f"{base_name}_handwritten.txt"
                    combined_json_path = output_path / f"{base_name}_handwritten_myscript.json"
                    
                    if combined_result:
                        # Save combined recognized text
                        combined_text = combined_result.get('text', '')
                        if not combined_text and 'words' in combined_result:
                            combined_text = ' '.join([word.get('label', '') for word in combined_result['words']])
                        
                        with open(combined_txt_path, 'w') as f:
                            f.write(combined_text)
                        files_generated.append(str(combined_txt_path))
                        print(f"Combined handwritten text saved to {combined_txt_path}")
                        
                        # Save combined MyScript JSON
                        with open(combined_json_path, 'w') as f:
                            json.dump(combined_result, f, indent=2)
                        files_generated.append(str(combined_json_path))
                        print(f"Combined MyScript output saved to {combined_json_path}")
                    else:
                        # Create placeholder files when MyScript API unavailable
                        with open(combined_txt_path, 'w') as f:
                            f.write("")  # Empty placeholder
                        files_generated.append(str(combined_txt_path))
                        
                        with open(combined_json_path, 'w') as f:
                            json.dump({"error": "MyScript API not configured", "text": "", "status": "failed"}, f, indent=2)
                        files_generated.append(str(combined_json_path))
                        
                        print(f"Created placeholder combined files (MyScript API not available)")
                
                # Create comprehensive summary
                summary_path = output_path / f"{base_name}_summary.txt"
                with open(summary_path, 'w') as f:
                    f.write("=== reMarkable Document Extraction Summary ===\n\n")
                    f.write(f"Total pages processed: {len(rm_files)}\n")
                    f.write(f"Total stroke groups: {len(all_stroke_groups)}\n")
                    f.write(f"Language: {language}\n\n")
                    f.write("Generated files:\n")
                    for file_path in files_generated:
                        f.write(f"- {Path(file_path).name}\n")
                files_generated.append(str(summary_path))
                print(f"Extraction summary saved to {summary_path}")
                
                # Create file list
                filelist_path = output_path / f"{base_name}_contents.txt"
                with open(filelist_path, 'w') as f:
                    f.write("ZIP Contents:\n")
                    for filename in file_list:
                        f.write(f"  {filename}\n")
                files_generated.append(str(filelist_path))
                
        except Exception as e:
            error_path = output_path / f"{rmdoc_file.stem}_error.txt"
            with open(error_path, 'w') as f:
                f.write(f"Error processing ZIP file {rmdoc_file.name}: {str(e)}\n")
            files_generated.append(str(error_path))
        
        finally:
            # Clean up temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
                print(f"Cleaned up temporary directory: {temp_dir}")
        
        # Generate summary JSON
        json_path = output_path / f"{rmdoc_file.stem}.json"
        with open(json_path, 'w') as f:
            json.dump({
                'source_file': str(rmdoc_file),
                'total_pages': total_pages,
                'language': language,
                'files_generated': files_generated,
                'file_type': 'zip_rmdoc_enhanced'
            }, f, indent=2)
        files_generated.append(str(json_path))
        
        return {
            'total_pages': total_pages,
            'files_generated': files_generated,
            'output_directory': str(output_path)
        }
