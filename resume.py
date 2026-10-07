"""Recreate the supplied A4 CV template with selectable text and working links."""
import html
import json
from pathlib import Path
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether


def documents(job, profile, folder, max_projects=5):
    folder.mkdir(parents=True, exist_ok=True)
    text = job['description'].lower()
    skills = sorted(profile['skills'], key=lambda s: s.lower() not in text)
    projects = sorted(profile['projects'], key=lambda p: -sum(s.lower() in text for s in p['skills']))[:max_projects]
    body = ParagraphStyle('Body', fontName='Helvetica', fontSize=10.5, leading=14, spaceAfter=0)
    heading = ParagraphStyle('Heading', fontName='Helvetica-Bold', fontSize=12, leading=16, spaceBefore=16, spaceAfter=4, keepWithNext=True)
    name_style = ParagraphStyle('Name', fontName='Helvetica-Bold', fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=16)
    escape = html.escape
    def para(t, style=body): return Paragraph(escape(t), style)
    def link(label, url): return f'<link href="{escape(url, quote=True)}" color="black">{escape(label)}</link>'
    story = [para(profile['name'].upper(), name_style), Paragraph(escape(profile['phone']) + '&nbsp;&nbsp;&nbsp;&nbsp;' + escape(profile['location']), body)]
    contacts = [escape(profile['email'])]
    for label, key in [('LinkedIn','linkedin'),('Github','github'),('Portfolio','website')]:
        if profile.get(key): contacts.append(link(label, profile[key]))
    story.append(Paragraph('&nbsp;&nbsp;&nbsp;&nbsp;'.join(contacts), body))
    objective = profile['summary'] + ' Seeking a ' + job['title'] + ' role.'
    sections = [('OBJECTIVE', [objective]), ('EDUCATION', profile['education']), ('EXPERTISE', [', '.join(skills) + '. Clean architecture, feature modularization and local-first AI applications.'])]
    lines = [profile['name'].upper(), profile['phone'] + '    ' + profile['location'], profile['email']]
    for title, paragraphs in sections:
        story.append(para(title, heading)); lines.append(title)
        for content in paragraphs:
            story.append(para(content)); lines.append(content)
    story.append(para('EXPERIENCE', heading)); lines.append('EXPERIENCE')
    for exp in profile['experience']:
        title = f"{exp['title']} - {exp['company']} - {exp['dates']}"
        bullets = ' '.join('• ' + b for b in exp['bullets'])
        story.append(KeepTogether([para(title), para(bullets), Spacer(1,6)]))
        lines.extend([title, bullets])
    story.append(para('PROJECTS', heading)); lines.append('PROJECTS')
    for project in projects:
        content = project['name'] + ' - ' + ' '.join(project['bullets'])
        story.append(para(content)); lines.append(content)
    story.append(para('LINKS', heading)); lines.append('LINKS')
    links = []
    for label,key in [('GitHub','github'),('Portfolio','website'),('LinkedIn','linkedin')]:
        if profile.get(key):
            links.append(label + ': ' + link(profile[key],profile[key]))
            lines.append(label + ': ' + profile[key])
    story.append(Paragraph('    '.join(links), body))
    SimpleDocTemplate(str(folder / 'resume.pdf'),pagesize=A4,leftMargin=50,rightMargin=50,topMargin=40,bottomMargin=44,title=profile['name']+' CV',author=profile['name']).build(story)
    (folder/'resume.txt').write_text('\n'.join(lines))
    matched = [s for s in profile['skills'] if s.lower() in text]
    letter = f"Dear {job['company']} hiring team,\n\nI am applying for the {job['title']} position. {profile['summary']} My relevant technical skills include {', '.join(matched) or profile['headline']}. My work includes {projects[0]['name']}: {projects[0]['bullets'][0]}\n\nPortfolio: {profile['website']}\n\nRegards,\n{profile['name']}"
    (folder/'cover-letter.txt').write_text(letter)
    (folder/'job.json').write_text(json.dumps(job,indent=2))
    return folder/'resume.pdf'
