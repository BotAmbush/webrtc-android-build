#include "stereo_opus_factory.h"

#include <memory>
#include <vector>
#include "api/audio_codecs/builtin_audio_decoder_factory.h"
#include "api/audio_codecs/opus/audio_decoder_opus.h"
#include "api/environment/environment.h"
#include "api/make_ref_counted.h"

namespace luma {
webrtc::SdpAudioFormat StereoOpusReceiveFormat() {
  // stereo asks the remote encoder for two channels. sprop-stereo is a sender
  // declaration; this receive-only endpoint does not invent a local sender.
  return {"opus",48000,2,{{"stereo","1"},{"ptime","10"},{"minptime","10"},
      {"maxptime","20"},{"useinbandfec","1"},{"usedtx","0"},
      {"maxaveragebitrate","128000"},{"maxplaybackrate","48000"}}};
}
namespace {
class StereoFactory : public webrtc::AudioDecoderFactory {
 public:
  StereoFactory() : builtin_(webrtc::CreateBuiltinAudioDecoderFactory()) {}
  std::vector<webrtc::AudioCodecSpec> GetSupportedDecoders() override {
    auto format=StereoOpusReceiveFormat();
    if(!IsSupportedDecoder(format))return {};
    webrtc::AudioCodecInfo info(48000,2,128000,6000,510000);
    info.allow_comfort_noise=false;info.supports_network_adaption=true;
    return {{std::move(format),info}};
  }
  bool IsSupportedDecoder(const webrtc::SdpAudioFormat& format) override {
    const auto config=webrtc::AudioDecoderOpus::SdpToConfig(format);
    // An absent/default/explicit-mono answer must not be called stereo. RTP
    // fallback remains the caller's explicit, joined transport decision.
    return builtin_&&config&&config->num_channels==2&&config->sample_rate_hz==48000
        &&builtin_->IsSupportedDecoder(format);
  }
  std::unique_ptr<webrtc::AudioDecoder> Create(const webrtc::Environment& env,
      const webrtc::SdpAudioFormat& format) override {
    if(!IsSupportedDecoder(format))return nullptr;
    auto decoder=builtin_->Create(env,format);
    if(!decoder||decoder->Channels()!=2||decoder->SampleRateHz()!=48000)return nullptr;
    return decoder;
  }
 private:
  const webrtc::scoped_refptr<webrtc::AudioDecoderFactory> builtin_;
};
}
webrtc::scoped_refptr<webrtc::AudioDecoderFactory> CreateStereoOpusDecoderFactory() {
  return webrtc::make_ref_counted<StereoFactory>();
}
}
