# StickAI for VS Code

Your desktop stickmen help with code. Yellow, or the character set as `coder` in
`characters.toml`, explains code in a side panel. The text appears while it is being written.

* **Select code → `Ctrl+Alt+E`**, or right-click → **Explain with StickAI**.
* **Nothing selected:** the extension explains the function or class under the cursor. Outside any function it explains the whole file (up to 800 lines).
* **From the StickAI chat:** while VS Code is in use, write "explain this code" or «объясни этот код».
* **Line references** like `L12` in the explanation are links. Click one to jump to that line.
* The status bar shows who is connected. Click it to explain the selection.

## Install

StickAI must be running. It links with the extension through `127.0.0.1` only, using a
secret token stored in `%LOCALAPPDATA%\StickAI\vscode.json`. No Node.js is needed:

```
python vscode/build.py --install
```

Or build `vscode/stickai.vsix` with `python vscode/build.py`. Then in VS Code open
**Extensions → … → Install from VSIX…**

## Privacy

Code is sent to Gemini only when you ask for an explanation. What gets sent:

* the selection, or the function or whole file;
* 40 lines around the selection, for context;
* the file name.

In the background the extension tells StickAI only the file name, the language and the selected line numbers, never code.
StickAI uses this to know what "this code" means in the chat.
