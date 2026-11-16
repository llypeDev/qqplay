<?php
/**
 * Lives and videos in the pages: the shortcodes, the links pasted on a line
 * of their own, and the address the page asks for the visitor's token.
 *
 *     [qqplay_live id="ID"]                  the live with its chat
 *     [qqplay_live id="ID" layout="player"]  or only the player, or "chat"
 *     [qqplay_video id="ID"]                 a video with its comments
 *     [qqplay_video id="ID" layout="player"] or only the player, or "comments"
 *     [qqplay url="https://portal/live/ID/"] either, by its address
 *
 * Other attributes: theme="dark|light", height="600" (the chat alone).
 *
 * @package QQplay
 */

defined( 'ABSPATH' ) || exit;

/**
 * What an id or a portal address points to.
 *
 * @param string $value An id, or the address of a live or a video.
 * @param string $kind  live or video, when the value is an id.
 * @return array{kind:string,id:string}|null
 */
function qqplay_target( $value, $kind = '' ) {
	$value = trim( (string) $value );
	if ( preg_match( '#^[A-Za-z0-9_-]{4,40}$#', $value ) ) {
		return in_array( $kind, array( 'live', 'video' ), true ) ? array(
			'kind' => $kind,
			'id'   => $value,
		) : null;
	}
	$path = (string) wp_parse_url( $value, PHP_URL_PATH );
	$id   = '([A-Za-z0-9_-]{4,40})';
	// The live's page, its frame, and the class link of an integration.
	if ( preg_match( "#/(?:embed/live|live)/{$id}/?$#", $path, $found ) || preg_match( "#/sala/[A-Za-z0-9_-]+/{$id}/?$#", $path, $found ) ) {
		return array(
			'kind' => 'live',
			'id'   => $found[1],
		);
	}
	if ( preg_match( "#/(?:embed|view)/{$id}/?$#", $path, $found ) || preg_match( "#/sala/[A-Za-z0-9_-]+/v/{$id}/?$#", $path, $found ) ) {
		return array(
			'kind' => 'video',
			'id'   => $found[1],
		);
	}
	return null;
}

/**
 * The address the page asks for the visitor's token. Only its path, so it is
 * always on the page's own origin and scheme: the visitor's cookie goes with
 * it, and a site set up with http:// behind an https:// proxy still works.
 *
 * @return string
 */
function qqplay_token_url() {
	$path = (string) wp_parse_url( admin_url( 'admin-ajax.php' ), PHP_URL_PATH );
	return add_query_arg( 'action', 'qqplay_token', '' !== $path ? $path : '/wp-admin/admin-ajax.php' );
}

/**
 * A note only the site's administrators see, where something is not set up.
 *
 * @param string $text What is missing.
 * @return string
 */
function qqplay_admin_note( $text ) {
	if ( ! current_user_can( 'manage_options' ) ) {
		return '';
	}
	return '<p class="qqplay-note"><strong>QQplay:</strong> ' . esc_html( $text ) . ' <a href="' . esc_url( admin_url( 'options-general.php?page=qqplay' ) ) . '">' . esc_html__( 'Open the settings', 'qqplay' ) . '</a></p>';
}

/**
 * The element the portal's script turns into the live or the video.
 *
 * @param string $kind live or video.
 * @param string $id   Its id on the portal.
 * @param array  $atts layout, theme, height.
 * @return string
 */
function qqplay_render( $kind, $id, $atts = array() ) {
	if ( ! qqplay_can_show() ) {
		return qqplay_admin_note( __( 'tell the plugin your portal\'s address and the integration\'s key.', 'qqplay' ) );
	}
	$layouts = 'live' === $kind ? array( 'both', 'player', 'chat' ) : array( 'both', 'player', 'comments' );
	$layout  = isset( $atts['layout'] ) ? strtolower( (string) $atts['layout'] ) : '';
	if ( 'video' === $kind && 'chat' === $layout ) {
		$layout = 'comments';
	}
	if ( ! in_array( $layout, $layouts, true ) ) {
		$layout = 'both';
	}

	$found = array(
		'class'                 => 'qqplay-embed',
		'data-qqplay-' . $kind  => $id,
		'data-layout'           => $layout,
		'data-school'           => qqplay_setting( 'key' ),
		'data-lang'             => strtolower( str_replace( '_', '-', determine_locale() ) ),
	);
	if ( qqplay_can_sign() ) {
		$found['data-token-url'] = qqplay_token_url();
	}
	$theme = isset( $atts['theme'] ) ? strtolower( (string) $atts['theme'] ) : '';
	if ( in_array( $theme, array( 'dark', 'light' ), true ) ) {
		$found['data-theme'] = $theme;
	}
	$height = isset( $atts['height'] ) ? (int) $atts['height'] : 0;
	if ( $height >= 200 && $height <= 2000 ) {
		$found['data-height'] = (string) $height;
	}
	$found = (array) apply_filters( 'qqplay_embed_attributes', $found, $kind, $id, $atts );

	// The portal's own script: no version in its address, it is always the
	// current one.
	wp_enqueue_script(
		'qqplay-sdk',
		qqplay_setting( 'portal' ) . '/embed/sdk.js',
		array(),
		null, // phpcs:ignore WordPress.WP.EnqueuedResourceParameters.MissingVersion
		array(
			'in_footer' => true,
			'strategy'  => 'async',
		)
	);

	$html = '<div';
	foreach ( $found as $name => $value ) {
		$html .= ' ' . esc_attr( $name ) . '="' . esc_attr( $value ) . '"';
	}
	$html .= '></div>';
	if ( ! qqplay_can_sign() ) {
		$html .= qqplay_admin_note( __( 'without the integration\'s secret, people watch but are not signed in.', 'qqplay' ) );
	}
	return $html;
}

/**
 * [qqplay_live], [qqplay_video] and [qqplay].
 *
 * @param array|string $atts    The attributes.
 * @param string|null  $content Not used.
 * @param string       $tag     Which of the three.
 * @return string
 */
function qqplay_shortcode( $atts, $content = null, $tag = 'qqplay' ) {
	$atts = shortcode_atts(
		array(
			'id'     => '',
			'url'    => '',
			'type'   => '',
			'layout' => '',
			'theme'  => '',
			'height' => '',
		),
		$atts,
		$tag
	);
	$kind = 'qqplay_live' === $tag ? 'live' : ( 'qqplay_video' === $tag ? 'video' : strtolower( (string) $atts['type'] ) );
	// An address says what it is by itself; an id needs the kind.
	$target = qqplay_target( '' !== $atts['url'] ? $atts['url'] : $atts['id'], $kind );
	if ( null === $target ) {
		return qqplay_admin_note( __( 'this shortcode needs the id or the address of a live or a video.', 'qqplay' ) );
	}
	return qqplay_render( $target['kind'], $target['id'], $atts );
}
add_shortcode( 'qqplay', 'qqplay_shortcode' );
add_shortcode( 'qqplay_live', 'qqplay_shortcode' );
add_shortcode( 'qqplay_video', 'qqplay_shortcode' );

/**
 * A portal address alone on a line: the same as its shortcode. The portal
 * is also registered as an oEmbed provider, which is what the editor's
 * preview shows.
 */
function qqplay_register_links() {
	$portal = qqplay_setting( 'portal' );
	if ( '' === $portal ) {
		return;
	}
	$pattern = '#^' . preg_quote( $portal, '#' ) . '/(?:embed/live|live|embed|view|sala/[A-Za-z0-9_-]+(?:/v)?)/[A-Za-z0-9_-]{4,40}/?(?:[?\#].*)?$#i';
	wp_embed_register_handler( 'qqplay', $pattern, 'qqplay_link_handler' );
	wp_oembed_add_provider( $portal . '/*', $portal . '/oembed/', false );
}
add_action( 'init', 'qqplay_register_links' );

/**
 * What a pasted portal address becomes.
 *
 * @param array  $matches The match of the pattern.
 * @param array  $attr    Width and height from the editor.
 * @param string $url     The address.
 * @return string
 */
function qqplay_link_handler( $matches, $attr, $url ) {
	$target = qqplay_target( $url );
	if ( null === $target ) {
		return '';
	}
	// A live comes with its chat; a video alone, as a pasted video does.
	return qqplay_render( $target['kind'], $target['id'], array( 'layout' => 'live' === $target['kind'] ? 'both' : 'player' ) );
}

/**
 * The token of whoever is signed in, for the page that asks.
 *
 * It answers each visitor for themselves and is never kept by a cache, so
 * the page around it can be. A visitor gets an empty token: the portal's
 * script then shows the live or the video without signing anyone in.
 */
function qqplay_ajax_token() {
	nocache_headers();
	$user  = wp_get_current_user();
	$token = ( $user instanceof WP_User && $user->exists() && qqplay_can_sign() ) ? qqplay_user_token( $user ) : '';
	wp_send_json( array( 'token' => $token ) );
}
add_action( 'wp_ajax_qqplay_token', 'qqplay_ajax_token' );
add_action( 'wp_ajax_nopriv_qqplay_token', 'qqplay_ajax_token' );
