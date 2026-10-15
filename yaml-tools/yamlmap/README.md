# yamlmap

Mind-map style web editor for a directory tree of YAML configs that lives in a git repository.

- the tree is drawn as a map of nodes and links: group → subgroup → directories → YAML files, plus the hosts of the subgroup
- click a file to edit it: key tree (typed values, add / delete) or raw text; comments, anchors and quoting survive
- every save shows a diff first, then writes the file and makes a git commit of that file only
- search across keys, values and names (files, directories, hosts), with regex and case options

## Expected layout

```
<base_dir>/
  <group>/
    <subgroup>/            only the subgroup named in the config is used
      hosts.txt            name of the file comes from the config; one host per line, '#' = comment
      app/service.yml      any sub directories with *.yml / *.yaml
      app/features/flags.yaml
```

## Run

```bash
pip install -r requirements.txt
cd /path/to/your/config-repo
cp /path/to/yamlmap/yamlmap.example.yml yamlmap.yml     # set subgroup and hosts_file
python /path/to/yamlmap/app.py --config yamlmap.yml     # http://127.0.0.1:8080/
```

Options can also be given on the command line: `--base-dir --subgroup --hosts-file --host --port`.
`python app.py --config yamlmap.yml` without `--config` uses `./yamlmap.yml` when it exists.

Try it on generated data: `python demo/make_demo.py /tmp/demo && cd /tmp/demo && python /path/to/yamlmap/app.py`.

Requirements: Python 3.9+, git, `git config user.name` / `user.email` set in the repository
(the tool refuses to start otherwise; set `git_commit: false` to edit without commits).

## Using it

| Action | How |
| --- | --- |
| Pan / zoom the map | drag the background / mouse wheel, buttons at the bottom right |
| Expand or collapse a branch | click a directory node or the small pill next to a node |
| Open a file | click a YAML node; click a host to jump to its line in the hosts file |
| Edit a value | change it in the **Ключи** tab (Enter or leaving the field records the change); type selector changes the type |
| Add / delete | `+ ключ` / `+ элемент` next to a container, `✕` on a row |
| Edit as text | **Текст** tab; switching with pending key edits folds them into the text first |
| Save | **Показать diff** (Ctrl+S) → check → **Сохранить и закоммитить**; the commit message is editable |
| Search | `/` focuses the box; scope checkboxes for keys / values / names; `.*` regex, `Aa` case |
| History | **История** tab lists the last commits of the file |

## Safety and behaviour

- Writes only inside `<base_dir>/<group>/<subgroup>/…`, only `*.yml|*.yaml` and the hosts file; `..`, absolute paths,
  hidden directories and symlinks leaving the base directory are rejected.
- Every save carries the hash of the file as it was opened. If the file changed on disk in the meantime the save is
  refused (409) and you are offered a reload, so nobody's edit is overwritten silently.
- The result is validated as YAML (duplicate keys are errors) before anything is written.
- A save commits only that file (`git add` + `git commit -- <file>`). If the commit fails (hook, identity) the file is
  restored to its previous content. A banner warns when the file already had uncommitted changes, because they would be
  part of the commit.
- Formatting: comments, anchors, quoting style, indentation, line endings (LF / CRLF) and an explicit `---` are kept.
  A file whose formatting cannot be reproduced exactly gets a warning; the diff shows what would change.
- Values inherited through a merge key (`<<: *anchor`) are marked `‹‹`. Editing one writes a local override;
  deleting is refused because it would silently do nothing.
- No authentication: the server listens on 127.0.0.1 and rejects cross-origin writes. Do not expose it to a network as is.

## Limits

- Renaming a key is delete + add (it moves to the end of its mapping).
- Multi-document YAML files and dates / custom tags are text-only.
- Anchors and aliases are not drawn as links on the map; the map shows the directory structure.
- The first scan parses every YAML file once (about 20-30 ms per 1000-key file); this runs in the background at start-up.

## Tests

```bash
pip install pytest httpx
python -m pytest tests
```
