import os
import glob

gtag_code = """
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-PLTCMFW4R7"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-PLTCMFW4R7');
    </script>
"""

templates_dir = r"c:\Users\Luciano\Documents\GitHub\youtubeA\templates"
html_files = glob.glob(os.path.join(templates_dir, "*.html"))

for file_path in html_files:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if "G-PLTCMFW4R7" not in content:
        # insert before </head>
        new_content = content.replace("</head>", gtag_code + "</head>")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Added gtag to {file_path}")
    else:
        print(f"gtag already in {file_path}")
