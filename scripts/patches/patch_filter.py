with open('agents/funding_agent.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''        outputs = []
        for doc in documents:
            analysis = analyze_document(doc)
            output_path = None
            try:
                output_path = save_markdown_output(doc, analysis, output_dir)
            except Exception:
                pass
            outputs.append(str(output_path) if output_path else "")''',
'''        outputs = []
        for doc in documents:
            analysis = analyze_document(doc)
            
            # Filter out CLOSED records from active results
            if analysis.status == "CLOSED_CALL":
                extra_warnings.append(f"Filtered out closed opportunity: {analysis.program_name}")
                continue
                
            output_path = None
            try:
                output_path = save_markdown_output(doc, analysis, output_dir)
            except Exception:
                pass
            outputs.append(str(output_path) if output_path else "")'''
)

with open('agents/funding_agent.py', 'w', encoding='utf-8') as f:
    f.write(text)
