# Code Signing & TestFlight Setup

This guide walks you through setting up automatic signed builds + TestFlight deployment from GitHub Actions. No Mac hardware needed day-to-day after initial setup.

## Prerequisites

- Apple Developer Program membership ($99/year) — [enroll here](https://developer.apple.com/programs/enroll/)
- One-time access to a Mac (or macOS VM) to generate the certificate and provisioning profile

## Step 1: Create an App ID

1. Go to [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/identifiers/list)
2. Click **+** → **App IDs** → **App**
3. Fill in:
   - Description: `Desktop Agent`
   - Bundle ID (Explicit): `com.bradtarver.DesktopAgent`
4. Under Capabilities, enable:
   - ✓ Background Modes
   - ✓ Camera (implicit via Info.plist)
5. Click **Register**

## Step 2: Create a Distribution Certificate

On Windows (or any machine with openssl):

```powershell
# Set openssl config (Git for Windows)
$env:OPENSSL_CONF = "C:\Program Files\Git\usr\ssl\openssl.cnf"

# Generate a certificate signing request
& "C:\Program Files\Git\usr\bin\openssl.exe" req -nodes -newkey rsa:2048 -keyout distribution.key -out CertificateSigningRequest.certSigningRequest -subj "/CN=Brad Tarver/C=US"
```

1. Go to [Certificates](https://developer.apple.com/account/resources/certificates/list)
2. Click **+** → **Apple Distribution**
3. Upload the `.certSigningRequest` file
4. Download the `.cer` file

```powershell
# Convert to .p12
& "C:\Program Files\Git\usr\bin\openssl.exe" x509 -in distribution.cer -inform DER -out distribution.pem
& "C:\Program Files\Git\usr\bin\openssl.exe" pkcs12 -export -out distribution.p12 -inkey distribution.key -in distribution.pem
```

## Step 3: Create a Provisioning Profile

1. Go to [Profiles](https://developer.apple.com/account/resources/profiles/list)
2. Click **+** → **App Store Connect** (under Distribution)
3. Select App ID: `com.bradtarver.DesktopAgent`
4. Select the distribution certificate you just created
5. Name it: `DesktopAgent AppStore`
6. Download the `.mobileprovision` file

## Step 4: Create an App Store Connect API Key

1. Go to [App Store Connect → Users and Access → Integrations → App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api)
2. Click **+** to generate a new key
3. Name: `GitHub Actions`
4. Access: **App Manager** (minimum needed for TestFlight uploads)
5. Download the `.p8` file (you can only download it once!)
6. Note the **Key ID** and **Issuer ID** shown on the page

## Step 5: Create the App in App Store Connect

1. Go to [App Store Connect → My Apps](https://appstoreconnect.apple.com/apps)
2. Click **+** → **New App**
3. Fill in:
   - Platform: iOS
   - Name: `Desktop Agent`
   - Primary Language: English
   - Bundle ID: select `com.bradtarver.DesktopAgent`
   - SKU: `desktop-agent`
4. Click **Create**

## Step 6: Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `CERTIFICATE_P12` | Base64 of your `.p12` file: `base64 -i distribution.p12` |
| `CERTIFICATE_PASSWORD` | The password you set when creating the .p12 |
| `KEYCHAIN_PASSWORD` | Any random string (e.g. `gh-actions-keychain-2026`) |
| `TEAM_ID` | Your 10-character Apple Developer Team ID (visible in Membership details) |
| `PROVISIONING_PROFILE` | Base64 of your `.mobileprovision`: `base64 -i DesktopAgent_AppStore.mobileprovision` |
| `PROVISIONING_PROFILE_NAME` | The name you gave the profile (e.g. `DesktopAgent AppStore`) |
| `ASC_KEY_ID` | The Key ID from Step 4 |
| `ASC_ISSUER_ID` | The Issuer ID from Step 4 |
| `ASC_PRIVATE_KEY` | The full contents of the `.p8` file |

### How to base64 encode on Windows (PowerShell):

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("distribution.p12"))
[Convert]::ToBase64String([IO.File]::ReadAllBytes("DesktopAgent_AppStore.mobileprovision"))
```

## Step 7: Trigger a Build

Either:
- Push any change to `iPadApp/` — auto-triggers the workflow
- Go to Actions → "Build iPad App" → "Run workflow" → set deploy_testflight to `true`

## Step 8: Install via TestFlight

1. Install [TestFlight](https://apps.apple.com/app/testflight/id899247664) on your iPad
2. After the build processes (usually 15-30 minutes), you'll get a notification
3. Open TestFlight → install the latest build

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "No signing certificate" | Re-check CERTIFICATE_P12 base64 encoding — no line breaks |
| "Provisioning profile doesn't match" | Ensure bundle ID in profile matches `com.bradtarver.DesktopAgent` exactly |
| "App Store Connect upload failed" | Verify ASC_KEY_ID and ASC_ISSUER_ID; ensure the .p8 key has App Manager access |
| "Missing compliance" | After first upload, go to App Store Connect → TestFlight → Manage Missing Compliance → select "None of the above" (no encryption beyond HTTPS) |

## One-Time Mac Access Options

You only need a Mac once to generate the certificate. Options:

- **MacinCloud** — rent a Mac for 1 hour (~$1-3)
- **GitHub Codespaces with macOS** — not available yet but coming
- **Friend's Mac** — just need Terminal for 5 minutes
- **Apple's web-based certificate generation** — works for some cert types directly in the browser at developer.apple.com

After the initial setup, everything runs in CI. You edit Swift on Windows, push, and TestFlight delivers the build to your iPad.
