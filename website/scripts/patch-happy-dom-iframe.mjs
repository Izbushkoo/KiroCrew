/**
 * Patches happy-dom's HTMLIFrameElement to NOT throw/log a DOMException when
 * iframe page loading is disabled AND handleDisabledFileLoadingAsSuccess is true.
 *
 * Root cause: happy-dom 20.x checks `handleDisabledFileLoadingAsSuccess` for
 * <script> and <link> elements but NOT for <iframe>. When disableIframePageLoading
 * is true, it unconditionally creates a DOMException, logs it to console.error,
 * and dispatches an error event — which vitest's fork worker counts as an
 * unhandled error (exit code 1) despite all test assertions passing.
 *
 * This patch makes the disabled-iframe path a silent no-op (just dispatch 'load')
 * when handleDisabledFileLoadingAsSuccess is also true, matching the behavior of
 * the script/CSS loading paths.
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const target = resolve(__dirname, '../node_modules/happy-dom/lib/nodes/html-iframe-element/HTMLIFrameElement.js')

const original = readFileSync(target, 'utf8')

const oldCode = `if (browserFrame.page.context.browser.settings.disableIframePageLoading) {
            const error = new window.DOMException(\`Failed to load iframe page "\${targetURL.href}". Iframe page loading is disabled.\`, DOMExceptionNameEnum.notSupportedError);
            browserFrame.page.console.error(error);
            this.dispatchEvent(new Event('error'));
            return;
        }`

const newCode = `if (browserFrame.page.context.browser.settings.disableIframePageLoading) {
            if (browserFrame.page.context.browser.settings.handleDisabledFileLoadingAsSuccess) {
                this.dispatchEvent(new Event('load'));
                return;
            }
            const error = new window.DOMException(\`Failed to load iframe page "\${targetURL.href}". Iframe page loading is disabled.\`, DOMExceptionNameEnum.notSupportedError);
            browserFrame.page.console.error(error);
            this.dispatchEvent(new Event('error'));
            return;
        }`

if (!original.includes(oldCode)) {
  if (original.includes('handleDisabledFileLoadingAsSuccess')) {
    console.log('patch-happy-dom-iframe: already patched, skipping.')
    process.exit(0)
  }
  console.error('patch-happy-dom-iframe: target code not found — happy-dom may have been updated.')
  process.exit(1)
}

writeFileSync(target, original.replace(oldCode, newCode))
console.log('patch-happy-dom-iframe: patched HTMLIFrameElement to respect handleDisabledFileLoadingAsSuccess.')
