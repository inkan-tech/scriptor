"""
Advanced MyScript Handwriting Analysis Module

Provides sophisticated analysis capabilities for MyScript JSON output including
multiple interpretations, contextual improvements, and spatial/temporal analysis.
"""

import json
import itertools
from typing import List, Dict, Any, Optional
from pathlib import Path

from .rm_utils import HandwritingAnalyzer, HandwritingInterpretation


class MyScriptAdvancedAnalyzer(HandwritingAnalyzer):
    """
    Enhanced analyzer with advanced features for MyScript handwriting recognition.
    
    Extends the base HandwritingAnalyzer with additional analysis capabilities
    including batch processing and enhanced reporting.
    """
    
    def __init__(self, myscript_data: Dict[str, Any]):
        super().__init__(myscript_data)
    
    def generate_detailed_report(self, max_interpretations: int = 8) -> Dict[str, Any]:
        """Generate a comprehensive analysis report."""
        interpretations = self.generate_interpretations(max_interpretations)
        flow_analysis = self.analyze_writing_flow()
        spatial_analysis = self.get_spatial_analysis()
        
        # Best interpretation summary
        best_interpretation = interpretations[0] if interpretations else None
        
        report = {
            'summary': {
                'original_recognition': self.original_text,
                'word_count': len(self.words),
                'has_temporal_data': bool(flow_analysis['writing_sequence']),
                'has_spatial_data': bool(spatial_analysis['words_by_position'])
            },
            'interpretations': [
                {
                    'rank': i + 1,
                    'text': interp.text,
                    'confidence': round(interp.confidence_score, 3),
                    'rationale': interp.rationale
                }
                for i, interp in enumerate(interpretations)
            ],
            'recommended_interpretation': {
                'text': best_interpretation.text if best_interpretation else self.original_text,
                'confidence': round(best_interpretation.confidence_score, 3) if best_interpretation else 0.0,
                'context_note': self._generate_context_note()
            },
            'writing_flow': flow_analysis,
            'spatial_analysis': spatial_analysis,
            'analysis_metadata': {
                'total_glyphs': flow_analysis['total_glyphs'],
                'line_breaks_detected': len(spatial_analysis['likely_line_breaks']),
                'interpretation_count': len(interpretations)
            }
        }
        
        return report
    
    def _generate_context_note(self) -> str:
        """Generate contextual note about the content."""
        text = self.original_text.lower()
        
        if any(term in text for term in ['toc', 'table of contents', 'add', 'document']):
            return "Appears to be handwritten notes about document editing or content organization"
        elif any(term in text for term in ['process', 'system', 'design']):
            return "Content appears to be technical or process-related notes"
        elif any(term in text for term in ['endpoint', 'api', 'code']):
            return "Appears to be technical documentation or API-related notes"
        else:
            return "General handwritten content"
    
    def export_analysis_to_file(self, output_path: str, format_type: str = 'json') -> str:
        """Export analysis results to a file."""
        report = self.generate_detailed_report()
        output_file = Path(output_path)
        
        if format_type.lower() == 'json':
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        elif format_type.lower() == 'txt':
            with open(output_file, 'w', encoding='utf-8') as f:
                self._write_text_report(f, report)
        else:
            raise ValueError(f"Unsupported format: {format_type}")
        
        return str(output_file)
    
    def _write_text_report(self, file, report: Dict[str, Any]):
        """Write a human-readable text report."""
        file.write("=== MyScript Advanced Handwriting Analysis Report ===\n\n")
        
        # Summary
        summary = report['summary']
        file.write(f"Original Recognition: {summary['original_recognition']}\n")
        file.write(f"Word Count: {summary['word_count']}\n")
        file.write(f"Has Temporal Data: {summary['has_temporal_data']}\n")
        file.write(f"Has Spatial Data: {summary['has_spatial_data']}\n\n")
        
        # Recommended interpretation
        rec = report['recommended_interpretation']
        file.write("=== Recommended Interpretation ===\n")
        file.write(f"Text: \"{rec['text']}\"\n")
        file.write(f"Confidence: {rec['confidence']:.3f}\n")
        file.write(f"Context: {rec['context_note']}\n\n")
        
        # All interpretations
        file.write("=== Alternative Interpretations ===\n")
        for interp in report['interpretations']:
            file.write(f"{interp['rank']}. \"{interp['text']}\"\n")
            file.write(f"   Confidence: {interp['confidence']:.3f}\n")
            file.write(f"   Rationale: {interp['rationale']}\n\n")
        
        # Writing flow
        flow = report['writing_flow']
        file.write("=== Writing Flow Analysis ===\n")
        file.write(f"Total Glyphs: {flow['total_glyphs']}\n")
        file.write(f"Time Span: {flow['time_span']}\n\n")
        
        # Spatial analysis
        spatial = report['spatial_analysis']
        file.write("=== Spatial Analysis ===\n")
        file.write("Words by position:\n")
        for word_info in spatial['words_by_position']:
            file.write(f"  '{word_info['word']}' at ({word_info['x']:.1f}, {word_info['y']:.1f})\n")
        
        if spatial['likely_line_breaks']:
            file.write(f"Likely line breaks after words: {spatial['likely_line_breaks']}\n")


def analyze_myscript_file(json_file_path: str, max_interpretations: int = 8) -> Dict[str, Any]:
    """
    Analyze a MyScript JSON file and return comprehensive results.
    
    Args:
        json_file_path: Path to the MyScript JSON file
        max_interpretations: Maximum number of interpretations to generate
    
    Returns:
        Dictionary containing comprehensive analysis results
    
    Raises:
        FileNotFoundError: If the JSON file doesn't exist
        json.JSONDecodeError: If the JSON file is malformed
    """
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    analyzer = MyScriptAdvancedAnalyzer(data)
    return analyzer.generate_detailed_report(max_interpretations)


def batch_analyze_directory(directory_path: str, pattern: str = "*_myscript.json") -> Dict[str, Any]:
    """
    Analyze all MyScript JSON files in a directory.
    
    Args:
        directory_path: Path to directory containing JSON files
        pattern: Glob pattern to match JSON files
    
    Returns:
        Dictionary with analysis results for each file
    """
    directory = Path(directory_path)
    results = {}
    
    for json_file in directory.glob(pattern):
        try:
            results[json_file.name] = analyze_myscript_file(str(json_file))
        except Exception as e:
            results[json_file.name] = {
                'error': str(e),
                'file_path': str(json_file)
            }
    
    return results


def generate_batch_summary(batch_results: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a summary of batch analysis results.
    
    Args:
        batch_results: Results from batch_analyze_directory
    
    Returns:
        Summary statistics and overview
    """
    total_files = len(batch_results)
    successful_analyses = sum(1 for result in batch_results.values() if 'error' not in result)
    failed_analyses = total_files - successful_analyses
    
    # Collect statistics from successful analyses
    total_words = 0
    total_interpretations = 0
    confidence_scores = []
    
    for result in batch_results.values():
        if 'error' not in result:
            total_words += result['summary']['word_count']
            total_interpretations += result['analysis_metadata']['interpretation_count']
            if result['interpretations']:
                confidence_scores.append(result['interpretations'][0]['confidence'])
    
    avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0
    
    return {
        'total_files': total_files,
        'successful_analyses': successful_analyses,
        'failed_analyses': failed_analyses,
        'total_words_processed': total_words,
        'total_interpretations_generated': total_interpretations,
        'average_top_confidence': round(avg_confidence, 3),
        'files_with_errors': [
            filename for filename, result in batch_results.items()
            if 'error' in result
        ]
    }
