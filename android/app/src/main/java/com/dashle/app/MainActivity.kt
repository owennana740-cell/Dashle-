package com.dashle.app

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import androidx.fragment.app.FragmentActivity
import androidx.biometric.BiometricPrompt
import java.util.concurrent.Executor

private const val PAYMENT_HOST="app.paydunya.com"
private const val CINETPAY_HOST="checkout.cinetpay.com"
private const val DASHLE_HOST="dashle.onrender.com"
private val OAUTH_HOSTS=setOf("github.com","accounts.google.com","api.notion.com")

class MainActivity : FragmentActivity() {
    private var webView:WebView?=null
    private var pendingHandoff:String?=null
    private lateinit var uiExecutor:Executor

    override fun onCreate(savedInstanceState:Bundle?) {
        super.onCreate(savedInstanceState)
        uiExecutor=mainExecutor
        pendingHandoff=intent?.data?.getQueryParameter("handoff")
        setContent{Surface(modifier=Modifier.fillMaxSize()){DashleWebApp{webView=it}}}
        onBackPressedDispatcher.addCallback(this,object:OnBackPressedCallback(true){
            override fun handleOnBackPressed(){if(webView?.canGoBack()==true)webView?.goBack() else finish()}
        })
    }

    override fun onNewIntent(intent:Intent){super.onNewIntent(intent);setIntent(intent);pendingHandoff=intent.data?.getQueryParameter("handoff");pendingHandoff?.let{token->webView?.loadUrl(BuildConfig.DASHLE_BASE_URL+"api/oauth/mobile/consume?handoff="+Uri.encode(token))}}

    override fun onResume(){super.onResume();CookieManager.getInstance().flush()}
    override fun onDestroy(){webView?.apply{stopLoading();webViewClient=WebViewClient();destroy()};webView=null;super.onDestroy()}

    @SuppressLint("SetJavaScriptEnabled")
    @Composable
    private fun DashleWebApp(onReady:(WebView)->Unit){
        AndroidView(modifier=Modifier.fillMaxSize(),factory={context->
            WebView(context).apply{
                settings.javaScriptEnabled=true
                settings.domStorageEnabled=true
                settings.databaseEnabled=true
                settings.cacheMode=WebSettings.LOAD_DEFAULT
                settings.allowFileAccess=false
                settings.allowContentAccess=true
                settings.javaScriptCanOpenWindowsAutomatically=true
                settings.mediaPlaybackRequiresUserGesture=false
                settings.userAgentString=settings.userAgentString+" DASHLE-Android/1.0"
                CookieManager.getInstance().setAcceptCookie(true)
                CookieManager.getInstance().setAcceptThirdPartyCookies(this,true)
                webChromeClient=WebChromeClient()
                addJavascriptInterface(DashleBridge(),"DashleAndroid")
                webViewClient=object:WebViewClient(){
                    override fun shouldOverrideUrlLoading(view:WebView,request:WebResourceRequest):Boolean{
                        val uri=request.url;val scheme=uri.scheme.orEmpty();val host=uri.host.orEmpty()
                        if((scheme=="http"||scheme=="https")&&host in OAUTH_HOSTS){
                            CustomTabsIntent.Builder().build().launchUrl(this@MainActivity,uri);return true
                        }
                        if(scheme=="http"||scheme=="https"){
                            val internal=host==DASHLE_HOST||host==PAYMENT_HOST||host==CINETPAY_HOST
                            if(internal)return false
                            return try{startActivity(Intent(Intent.ACTION_VIEW,uri));true}catch(_:Exception){false}
                        }
                        return try{startActivity(Intent(Intent.ACTION_VIEW,uri));true}catch(_:Exception){true}
                    }
                }
                loadUrl(BuildConfig.DASHLE_BASE_URL)
                pendingHandoff?.let{token->postDelayed({loadUrl(BuildConfig.DASHLE_BASE_URL+"api/oauth/mobile/consume?handoff="+Uri.encode(token));pendingHandoff=null},800)}
                onReady(this)
            }
        },update={})
    }

    inner class DashleBridge{
        @JavascriptInterface fun confirmAction(token:String){
            val current=webView?.url?.let{Uri.parse(it).host}
            if(current!=DASHLE_HOST)return
            val prompt=BiometricPrompt(this@MainActivity,uiExecutor,object:BiometricPrompt.AuthenticationCallback(){
                override fun onAuthenticationSucceeded(result:BiometricPrompt.AuthenticationResult){
                    super.onAuthenticationSucceeded(result)
                    webView?.post{webView?.evaluateJavascript("window.DashleAndroidBiometricResult && window.DashleAndroidBiometricResult(true,"+org.json.JSONObject.quote(token)+");",null)}
                }
                override fun onAuthenticationError(code:Int,message:CharSequence){
                    webView?.post{webView?.evaluateJavascript("window.DashleAndroidBiometricResult && window.DashleAndroidBiometricResult(false);",null)}
                }
            })
            val info=BiometricPrompt.PromptInfo.Builder()
                .setTitle("Confirmer l’action Dashle")
                .setSubtitle("Action sur ton compte externe")
                .setDescription("Confirme cette action précise pour continuer.")
                .setNegativeButtonText("Annuler")
                .build()
            prompt.authenticate(info)
        }
    }
}
