plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}
val dashleBaseUrl = providers.gradleProperty("dashleBaseUrl").orElse("https://dashle.onrender.com/").get().replace("\\", "\\\\").replace("\"", "\\\"")
android {
    namespace = "com.dashle.app"
    compileSdk = 37
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
    debugImplementation(libs.androidx.compose.ui.tooling)
}