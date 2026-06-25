# Contributing

Thanks for your interest in **Alexa Media Player**!

## Bug reports

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml).

## Feature requests

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.yml).

## Development setup

```bash
python -m pip install -r requirements_dev.txt
pre-commit install        # or: prek install
bash tests/setup.sh       # creates the test custom_component symlink
```

## Pull requests

1. Create a dedicated branch: `git checkout -b feat/my-feature`
2. Make your change and add/update tests
3. Run the checks locally:
   - `ruff check .`
   - `ruff format --check .`
   - `pytest`
4. Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, …)
5. Open a pull request against `main`

Releases are handled automatically by [release-please](https://github.com/googleapis/release-please):
merging the release PR publishes a new version and bumps `manifest.json`.

## Translations

Translations live in `custom_components/alexa_media/translations/`. Add or update the relevant
`<lang>.json` file and keep `strings.json` in sync.

## License

By contributing, you agree that your contribution is licensed under the [Apache-2.0](LICENSE) license.
