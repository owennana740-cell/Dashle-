plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}
val dashleBaseUrl = providers.gradleProperty("dashleBaseUrl").orElse("https://dashle.onrender.com/").get().replace("\\", "\\\\").replace("\"", "\\\"")

val releaseStoreFile = providers.environmentVariable("DASHLE_RELEASE_STORE_FILE").orNull
val releaseStorePassword = providers.environmentVariable("DASHLE_RELEASE_STORE_PASSWORD").orNull
val releaseKeyAlias = providers.environmentVariable("DASHLE_RELEASE_KEY_ALIAS").orNull
val releaseKeyPassword = providers.environmentVariable("DASHLE_RELEASE_KEY_PASSWORD").orNull
val releaseSigningReady = listOf(releaseStoreFile, releaseStorePassword, releaseKeyAlias, releaseKeyPassword).all { !it.isNullOrBlank() }

android {
    namespace = "com.dashle.app"
    compileSdk = 37
    signingConfigs {
        create("release") {
            if (releaseSigningReady) {
                storeFile = file(releaseStoreFile!!)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }
    buildTypes {
        getByName("release") {
            if (releaseSigningReady) signingConfig = signingConfigs.getByName("release")
        }
    }
    defaultConfig {
        applicationId = "com.dashle.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0.0"
        buildConfigField("String", "DASHLE_BASE_URL", "\"$dashleBaseUrl\"")
    }
    buildFeatures { buildConfig = true; compose = true }
}
dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.webkit)
    implementation(libs.androidx.browser)
    implementation(libs.androidx.biometric)
    debugImplementation(libs.androidx.compose.ui.tooling)
}