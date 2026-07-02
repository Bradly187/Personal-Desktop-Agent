"use strict";
// Lazy file tree: drive roots, children fetched on first expand, inline error
// rows for unreadable directories (never crashes the tree).

const FsTree = (() => {
  let onOpenFile = () => {};
  let selectedRow = null;

  function makeRow({ label, icon, isDir, indentParent }) {
    const row = document.createElement("div");
    row.className = "tree-row";
    row.setAttribute("role", "treeitem");
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = isDir ? "▶" : "";
    const iconEl = document.createElement("span");
    iconEl.className = "icon";
    iconEl.textContent = icon;
    const name = document.createElement("span");
    name.textContent = label;
    row.append(chevron, iconEl, name);
    indentParent.appendChild(row);
    return row;
  }

  function makeErrorRow(message, parent) {
    const row = document.createElement("div");
    row.className = "tree-row error-row";
    row.textContent = `⚠ ${message}`;
    parent.appendChild(row);
  }

  function select(row) {
    if (selectedRow) selectedRow.classList.remove("selected");
    selectedRow = row;
    row.classList.add("selected");
  }

  function addDirNode(container, path, label) {
    const row = makeRow({ label, icon: "📁", isDir: true, indentParent: container });
    row.title = path;
    const childBox = document.createElement("div");
    childBox.className = "tree-children";
    childBox.hidden = true;
    container.appendChild(childBox);

    let loaded = false;
    row.addEventListener("click", async () => {
      select(row);
      const expanding = childBox.hidden;
      childBox.hidden = !expanding;
      row.classList.toggle("expanded", expanding);
      if (expanding && !loaded) {
        loaded = true;
        const result = await window.agent.fs.listDir(path);
        if (result.error) {
          makeErrorRow(result.error, childBox);
          return;
        }
        for (const item of result.items) {
          if (item.isDir) {
            addDirNode(childBox, item.path, item.name);
          } else {
            const fileRow = makeRow({ label: item.name, icon: "📄", isDir: false, indentParent: childBox });
            fileRow.title = item.path;
            fileRow.addEventListener("click", () => {
              select(fileRow);
              onOpenFile(item.path);
            });
          }
        }
        if (result.items.length === 0) makeErrorRow("empty", childBox);
      }
    });
  }

  async function init(openFileCallback) {
    onOpenFile = openFileCallback;
    const tree = document.getElementById("tree");
    const drives = await window.agent.fs.listDrives();
    for (const drive of drives) addDirNode(tree, drive, drive);
  }

  return { init };
})();
