with open('core/structured_sources.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''    for x in issues[:limit]:
        if 'pull_request' not in x: text.append(f"Issue: {x.get('title','')} | {x.get('html_url','')} | updated {x.get('updated_at','')}")''',
'''    # Filecoin Semantics & general Github semantics:
    # Do not treat every issue as a grant opportunity.
    # We classify them based on labels and state.
    for x in issues[:limit]:
        if 'pull_request' in x: continue
        labels = [l.get('name', '').lower() for l in x.get('labels', [])]
        state = x.get('state', 'open').lower()
        title = x.get('title', '').lower()
        
        is_grant_request = "grant" in title or "rfp" in title or "open grant" in labels
        is_informational = "question" in labels or "discussion" in labels
        is_approved = "approved" in labels or "funded" in labels
        is_rejected = "rejected" in labels or "closed" in state and not is_approved
        
        status_label = "Open Grant Request" if (is_grant_request and state == "open") else \\
                       "Approved Grant" if is_approved else \\
                       "Closed/Rejected Request" if (is_grant_request and state == "closed") else \\
                       "Informational Issue" if is_informational else \\
                       "Issue"
                       
        text.append(f"{status_label}: {x.get('title','')} | {x.get('html_url','')} | updated {x.get('updated_at','')}")'''
)

with open('core/structured_sources.py', 'w', encoding='utf-8') as f:
    f.write(text)
